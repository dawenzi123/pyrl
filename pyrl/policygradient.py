from __future__ import absolute_import, division

import datetime
import sys
from   collections import OrderedDict

import numpy as np

import theano
from   theano import tensor

from .         import tasktools, theanotools, utils
from .networks import Networks
from .sgd      import Adam

DEBUG = True

class PolicyGradient(object):
    def __init__(self, Task, config_or_savefile, seed, dt=None, load='best'):
        self.task = Task()

        #=================================================================================
        # Network setup
        #=================================================================================

        if isinstance(config_or_savefile, str):
            #-----------------------------------------------------------------------------
            # Existing model
            #-----------------------------------------------------------------------------

            savefile = config_or_savefile
            save = utils.load(savefile)
            self.save   = save
            self.config = save['config']

            # Model summary
            print("[ PolicyGradient ]")
            print("  Loading {}".format(savefile))
            print("  Last saved after {} iterations.".format(save['iter']))

            # Performance
            items = OrderedDict()
            items['Best reward'] = '{} (after {} iterations)'.format(save['best_reward'],
                                                                     save['best_iter'])
            if save['best_perf'] is not None:
                items.update(save['best_perf'].display(output=False))
            utils.print_dict(items)

            # Network type
            Network = Networks[self.config['network_type']]

            # Time step
            print("dt = {}".format(dt))
            self.dt = dt
            if self.dt is None:
                self.dt = self.config['dt']

            # Leak
            alpha = self.dt/self.config['tau']

            # Which parameters to load?
            if load == 'best':
                params_p = save['best_policy_params']
                params_b = save['best_baseline_params']
            elif load == 'current':
                params_p = save['current_policy_params']
                params_b = save['current_baseline_params']
            else:
                raise ValueError(load)

            # Policy network
            self.policy_config = save['policy_config']
            self.policy_config['alpha'] = alpha
            self.policy_net = Network(self.policy_config, params=params_p)

            # Baseline network
            self.baseline_config = save['baseline_config']
            self.baseline_config['alpha'] = alpha
            self.baseline_net = Network(self.baseline_config, params=params_b)
        else:
            #-----------------------------------------------------------------------------
            # Create new model.
            #-----------------------------------------------------------------------------

            config = config_or_savefile
            self.config = config

            # Network type
            Network = Networks[config['network_type']]

            # Time step
            print("dt = {}".format(dt))
            self.dt = dt
            if self.dt is None:
                self.dt = config['dt']

            # Leak
            alpha = self.dt/config['tau']

            # Policy network
            self.policy_config = {
                'network_type': config['network_type'],
                'Nin':          config['Nin'],
                'N':            config['N'],
                'Nout':         config['Nout'],
                'f_out':        'softmax',
                #'f_out':        'linear',
                'fix':          config['fix'],
                'L2_r':         config['L2_r'],
                'L1_Wrec':      config['L1_Wrec'],
                'L2_Wrec':      config['L2_Wrec'],
                'alpha':        alpha
                }
            self.policy_net = Network(self.policy_config, seed=config['policy_seed'])

            # Baseline network
            self.baseline_config = {
                'network_type': config['network_type'],
                'Nin':          self.policy_net.N + len(config['actions']),
                'N':            config['N'],
                'Nout':         1,
                'f_out':        'linear',
                'fix':          config['fix'],
                'L2_r':         config['baseline_L2_r'],
                'L1_Wrec':      config['L1_Wrec'],
                'L2_Wrec':      config['L2_Wrec'],
                'alpha':        alpha
                }
            self.baseline_net = Network(self.baseline_config,
                                        seed=config['baseline_seed'])

        #=================================================================================
        # PG setup
        #=================================================================================

        # Network structure
        self.Nin  = self.config['Nin']
        self.N    = self.config['N']
        self.Nout = self.config['Nout']

        # Number of actions
        self.n_actions = len(self.config['actions'])

        # Recurrent noise, scaled by `2*tau/dt`
        self.scaled_var_rec = (2*self.config['tau']/self.dt) * self.config['var_rec']

        # Run trials continuously?
        self.mode = self.config['mode']
        if self.mode == 'continuous':
            self.step_0_states = self.policy_net.func_step_0(True)

        # Maximum length of a trial
        self.Tmax = int(self.config['tmax']/self.config['dt']) + 1

        # Reward on aborted trials
        self.R_ABORTED = self.config['R_ABORTED']

        # Random number generator
        print("Creating RNG with seed {}".format(seed))
        self.rng = np.random.RandomState(seed)

        # Compile functions
        self.step_0          = self.policy_net.func_step_0()
        self.step_t          = self.policy_net.func_step_t()
        self.baseline_step_0 = self.baseline_net.func_step_0()
        self.baseline_step_t = self.baseline_net.func_step_t()

        # Performance
        self.Performance = self.config['Performance']

    def make_noise(self, size, var=0):
        if var > 0:
            return theanotools.asarray(self.rng.normal(scale=np.sqrt(var), size=size))
        return theanotools.zeros(size)

    def run_trials(self, trials, init=None, init_b=None,
                   return_states=False, perf=None, task=None, progress_bar=False):
        if isinstance(trials, list):
            n_trials = len(trials)
        else:
            n_trials = trials
            trials   = []

        if return_states:
            run_value_network = True
        else:
            run_value_network = False

        # Storage
        U   = theanotools.zeros((self.Tmax, n_trials, self.Nin))
        Z   = theanotools.zeros((self.Tmax, n_trials, self.Nout))
        A   = theanotools.zeros((self.Tmax, n_trials, self.n_actions))
        R   = theanotools.zeros((self.Tmax, n_trials))
        M   = theanotools.zeros((self.Tmax, n_trials))
        Z_b = theanotools.zeros((self.Tmax, n_trials))

        # Noise
        Q   = self.make_noise((self.Tmax, n_trials, self.policy_net.noise_dim),
                               self.scaled_var_rec)
        Q_b = self.make_noise((self.Tmax, n_trials, self.baseline_net.noise_dim),
                               self.scaled_var_rec)

        # Firing rates
        if return_states:
            r_policy = theanotools.zeros((self.Tmax, n_trials, self.N))
            r_value  = theanotools.zeros((self.Tmax, n_trials, self.baseline_net.N))

        # Keep track of initial conditions
        if self.mode == 'continuous':
            x0   = theanotools.zeros((n_trials, self.policy_net.N))
            x0_b = theanotools.zeros((n_trials, self.baseline_net.N))
        else:
            x0   = None
            x0_b = None

        # Performance
        if perf is None:
            perf = self.Performance()

        # Setup progress bar
        if progress_bar:
            progress_inc  = max(int(n_trials/50), 1)
            progress_half = 25*progress_inc
            if progress_half > n_trials:
                progress_half = -1
            utils.println("[ PolicyGradient.run_trials ] ")

        for n in xrange(n_trials):
            if progress_bar and n % progress_inc == 0:
                if n == 0:
                    utils.println("0")
                elif n == progress_half:
                    utils.println("50")
                else:
                    utils.println("|")

            # Initialize trial
            if hasattr(self.task, 'start_trial'):
                self.task.start_trial()

            # Generate trials
            if n < len(trials):
                trial = trials[n]
            else:
                trial = task.get_condition(self.rng, self.dt)
                trials.append(trial)

            #-----------------------------------------------------------------------------
            # Time t = 0
            #-----------------------------------------------------------------------------

            t = 0
            if init is None:
                z_t,   x_t   = self.step_0()
                z_t_b, x_t_b = self.baseline_step_0()
            else:
                z_t,   x_t   = init
                z_t_b, x_t_b = init_b
            Z[t,n]   = z_t
            Z_b[t,n] = z_t_b

            # Save initial condition
            if x0 is not None:
                x0[n]   = x_t
                x0_b[n] = x_t_b

            # Save states
            if return_states:
                r_policy[t,n] = self.policy_net.firing_rate(x_t)
                r_value[t,n]  = self.baseline_net.firing_rate(x_t_b)

            # Select action
            a_t = theanotools.choice(self.rng, self.Nout, p=np.reshape(z_t, (self.Nout,)))
            A[t,n,a_t] = 1

            #a_t = self.rng.normal(np.reshape(z_t, (self.Nout,)), self.sigma)
            #A[t,n,0] = a_t

            # Trial step
            U[t,n], R[t,n], status = self.task.get_step(self.rng, self.dt,
                                                        trial, t+1, a_t)
            u_t    = U[t,n]
            M[t,n] = 1

            # Noise
            q_t   = Q[t,n]
            q_t_b = Q_b[t,n]

            #-----------------------------------------------------------------------------
            # Time t > 0
            #-----------------------------------------------------------------------------

            for t in xrange(1, self.Tmax):
                # Aborted episode
                if not status['continue']:
                    break

                # Policy
                z_t, x_t = self.step_t(u_t[None,:], q_t[None,:], x_t[None,:])
                Z[t,n] = z_t

                # Baseline
                r_t = self.policy_net.firing_rate(x_t)
                u_t_b = np.concatenate((r_t, A[t-1,n]), axis=-1)
                z_t_b, x_t_b = self.baseline_step_t(u_t_b[None,:],
                                                    q_t_b[None,:],
                                                    x_t_b[None,:])
                Z_b[t,n] = z_t_b

                # Firing rates
                if return_states:
                    r_policy[t,n] = self.policy_net.firing_rate(x_t)
                    r_value[t,n]  = self.baseline_net.firing_rate(x_t_b)

                # Select action
                a_t = theanotools.choice(self.rng, self.Nout,
                                         p=np.reshape(z_t, (self.Nout,)))
                A[t,n,a_t] = 1

                #a_t = self.rng.normal(np.reshape(z_t, (self.Nout,)), self.sigma)
                #A[t,n,0] = a_t

                # Trial step
                if t < self.Tmax-1:
                    U[t,n], R[t,n], status = self.task.get_step(self.rng, self.dt,
                                                                trial, t+1, a_t)
                else:
                    U[t,n] = 0
                    R[t,n] = self.R_ABORTED
                    status = {'continue': False}
                u_t    = U[t,n]
                M[t,n] = 1

                # Noise
                q_t   = Q[t,n]
                q_t_b = Q_b[t,n]

            #-----------------------------------------------------------------------------

            # Update performance
            perf.update(trial, status)

            # Save next state if necessary
            if self.mode == 'continuous':
                init   = self.step_t(u_t[None,:], q_t[None,:], x_t[None,:])
                init_b = self.baseline_step_t(u_t_b[None,:], q_t_b[None,:], x_t_b[None,:])
        if progress_bar:
            print("100")

        #---------------------------------------------------------------------------------

        rvals = [U, Q, Q_b, Z, Z_b, A, R, M, init, init_b, x0, x0_b, perf]
        if return_states:
            rvals += [r_policy, r_value]

        return rvals

    def func_update_policy(self, Tmax, use_x0=False, accumulators=None):
        U = tensor.tensor3('U')
        noise = tensor.tensor3('noise')

        if use_x0:
            x0_ = tensor.matrix('x0_')
        else:
            x0  = self.policy_net.params['x0']
            x0_ = tensor.alloc(x0, U.shape[1], x0.shape[0])

        z_0  = self.policy_net.get_outputs_0(x0_)
        r, z = self.policy_net.get_outputs(U, noise, x0_)

        # Learning rate
        lr = tensor.scalar('lr')

        A = tensor.tensor3('A')
        R = tensor.matrix('R')
        b = tensor.matrix('b')
        M = tensor.matrix('M')

        logpi_0 = tensor.sum(tensor.log(z_0)*A[0], axis=-1)*M[0]
        logpi_t = tensor.sum(tensor.log(z)*A[1:],  axis=-1)*M[1:]

        #def f(x):
        #    return -x**2/2/self.sigma**2

        #logpi_0 = tensor.sum(f(A[0] - z_0), axis=-1)*M[0]
        #logpi_t = tensor.sum(f(A[1:] - z), axis=-1)*M[1:]

        # Enforce causality
        Mcausal = theanotools.zeros((Tmax-1, Tmax-1))
        for i in xrange(Mcausal.shape[0]):
            Mcausal[i,i:] = 1
        Mcausal = theanotools.shared(Mcausal, 'Mcausal')

        J0 = logpi_0*R[0]
        J0 = tensor.mean(J0)
        J  = (logpi_t.T).dot(Mcausal).dot(R[1:]*M[1:])
        J  = tensor.nlinalg.trace(J)/J.shape[0]

        J += J0

        # Second term
        Jb0 = logpi_0*b[0]
        Jb0 = tensor.mean(Jb0)
        Jb  = logpi_t*b[1:]
        Jb  = tensor.mean(tensor.sum(Jb, axis=0))

        J -= Jb0 + Jb

        # Objective function
        obj = -J + self.policy_net.get_regs(x0_, r, M)

        # SGD
        self.policy_sgd = Adam(self.policy_net.trainables, accumulators=accumulators)
        if self.policy_net.type == 'simple':
            i = self.policy_net.index('Wrec')
            grads = tensor.grad(obj, self.policy_net.trainables)
            grads[i] += self.policy_net.get_dOmega_dWrec(-J, r)
            norm, grads, updates = self.policy_sgd.get_updates(obj, lr, grads=grads)
        else:
            norm, grads, updates = self.policy_sgd.get_updates(obj, lr)

        if use_x0:
            args = [x0_]
        else:
            args = []
        args += [U, noise, A, R, b, M, lr]

        return theano.function(args, norm, updates=updates)

    def func_update_baseline(self, use_x0=False, accumulators=None):
        U  = tensor.tensor3('U')
        R  = tensor.matrix('R')
        R_ = R.reshape((R.shape[0], R.shape[1], 1))

        noise  = tensor.tensor3('noise')

        if use_x0:
            x0_ = tensor.matrix('x0_')
        else:
            x0  = self.baseline_net.params['x0']
            x0_ = tensor.alloc(x0, U.shape[1], x0.shape[0])

        z_0   = self.baseline_net.get_outputs_0(x0_)
        r, z  = self.baseline_net.get_outputs(U, noise, x0_)
        z_all = tensor.concatenate([z_0.reshape((1, z_0.shape[0], z_0.shape[1])), z],
                                   axis=0)

        # Learning rate
        lr = tensor.scalar('lr')

        # Reward prediction error
        M  = tensor.matrix('M')
        L2 = tensor.sum((tensor.sqr(z_all[:,:,0] - R))*M)/tensor.sum(M)

        # Objective function
        obj = L2 + self.baseline_net.get_regs(x0_, r, M)

        # SGD
        self.baseline_sgd = Adam(self.baseline_net.trainables, accumulators=accumulators)
        if self.baseline_net.type == 'simple':
            i = self.baseline_net.index('Wrec')
            grads = tensor.grad(obj, self.baseline_net.trainables)
            grads[i] += self.baseline_net.get_dOmega_dWrec(L2, r)
            norm, grads, updates = self.baseline_sgd.get_updates(obj, lr, grads=grads)
        else:
            norm, grads, updates = self.baseline_sgd.get_updates(obj, lr)

        if use_x0:
            args = [x0_]
        else:
            args = []
        args += [U, noise, R, M, lr]

        return theano.function(args, [z_all[:,:,0], norm], updates=updates)

    def train(self, savefile, recover=False):
        """
        Train network.

        """
        #=================================================================================
        # Parameters
        #=================================================================================

        max_iter     = self.config['max_iter']
        lr           = self.config['lr']
        baseline_lr  = self.config['baseline_lr']
        n_gradient   = self.config['n_gradient']
        n_validation = self.config['n_validation']
        checkfreq    = self.config['checkfreq']

        if self.mode == 'continuous':
            print("[ PolicyGradient.train ] Continuous mode.")
            use_x0 = True
        else:
            use_x0 = False

        # Print settings
        items = OrderedDict()
        items['Network type']             = self.config['network_type']
        items['N']                        = self.config['N']
        items['var_rec']                  = self.config['var_rec']
        items['dt']                       = self.dt
        items['Learning rate (policy)']   = self.config['lr']
        items['Learning rate (value)']    = self.config['baseline_lr']
        items['Max time steps']           = self.Tmax
        items['Num. trials (gradient)']   = self.config['n_gradient']
        items['Num. trials (validation)'] = self.config['n_validation']
        utils.print_dict(items)

        #=================================================================================
        # Setup
        #=================================================================================

        if recover:
            print("Resume training.")
            update_policy   = self.func_update_policy(self.Tmax, use_x0,
                                                      accumulators=self.save['net_sgd'])
            update_baseline = self.func_update_baseline(use_x0,
                                                        accumulators=self.save['baseline_sgd'])

            # Resume training from here
            iter_start = self.save['iter']
            print("Last saved was after {} iterations.".format(self.save['iter']))

            # Random number generator
            print("Resetting RNG state")
            self.rng.set_state(self.save['rng_state'])

            # Keep track of best results
            best_iter            = self.save['best_iter']
            best_reward          = self.save['best_reward']
            best_perf            = self.save['best_perf']
            best_params          = self.save['best_policy_params']
            best_baseline_params = self.save['best_baseline_params']

            # Initial states
            init   = self.save['init']
            init_b = self.save['init_b']

            # Training history
            perf             = self.save['perf']
            training_history = self.save['training_history']
            trials_tot       = self.save['trials_tot']
        else:
            update_policy   = self.func_update_policy(self.Tmax, use_x0)
            update_baseline = self.func_update_baseline(use_x0)

            # Start training from here
            iter_start = 0

            # Keep track of best results
            best_iter    = -1
            best_reward  = -np.inf
            best_perf    = None
            best_params  = self.policy_net.get_values()
            best_baseline_params = self.baseline_net.get_values()

            # Initial states
            init   = None
            init_b = None

            # Performance history
            perf             = None
            training_history = []
            trials_tot       = 0

        #---------------------------------------------------------------------------------
        # Train
        #---------------------------------------------------------------------------------

        if hasattr(self.task, 'start_session'):
            self.task.start_session(self.rng)

        grad_norms_policy   = []
        grad_norms_baseline = []

        tstart = datetime.datetime.now()
        try:
            for iter in xrange(iter_start, max_iter):
                if iter % checkfreq == 0:
                    if n_validation > 0:
                        #-----------------------------------------------------------------
                        # Validation
                        #-----------------------------------------------------------------

                        # Report
                        elapsed = utils.elapsed_time(tstart)
                        print("After {} iterations ({})".format(iter, elapsed))

                        # RNG state
                        rng_state = self.rng.get_state()

                        # Trials
                        trials = [self.task.get_condition(self.rng, self.dt)
                                  for i in xrange(n_validation)]

                        # Run trials
                        (U, Q, Q_b, Z, Z_b, A, R, M, init_, init_b_, x0_, x0_b_,
                         perf_) = self.run_trials(trials)

                        # Save
                        mean_reward = np.sum(R*M)/n_validation
                        record = {
                            'iter':        iter,
                            'mean_reward': mean_reward,
                            'n_trials':    trials_tot,
                            'perf':        perf_
                            }
                        if mean_reward > best_reward:
                            best_iter   = iter
                            best_reward = mean_reward
                            best_perf   = perf_
                            best_params          = self.policy_net.get_values()
                            best_baseline_params = self.baseline_net.get_values()

                            record['new_best'] = True
                            training_history.append(record)
                        else:
                            record['new_best'] = False
                            training_history.append(record)

                        # Save
                        save = {
                            'iter':                    iter,
                            'config':                  self.config,
                            'policy_config':           self.policy_net.config,
                            'baseline_config':         self.baseline_net.config,
                            'current_policy_params':   self.policy_net.get_values(),
                            'current_baseline_params': self.baseline_net.get_values(),
                            'best_iter':               best_iter,
                            'best_reward':             best_reward,
                            'best_perf':               best_perf,
                            'best_policy_params':      best_params,
                            'best_baseline_params':    best_baseline_params,
                            'rng_state':               rng_state,
                            'init':                    init,
                            'init_b':                  init_b,
                            'perf':                    perf,
                            'training_history':        training_history,
                            'trials_tot':              trials_tot,
                            'net_sgd':                 self.policy_sgd.get_values(),
                            'baseline_sgd':            self.baseline_sgd.get_values()
                            }
                        utils.save(savefile, save)

                        # Reward
                        items = OrderedDict()
                        items['Best reward'] = '{} (iteration {})'.format(best_reward,
                                                                          best_iter)
                        items['Mean reward'] = '{}'.format(mean_reward)

                        # Performance
                        if perf_ is not None:
                            items.update(perf_.display(output=False))

                        # Value prediction error
                        V = np.zeros_like(R)
                        for k in xrange(V.shape[0]):
                            V[k] = np.sum(R[k:]*M[k:], axis=0)
                        error = np.sqrt(np.sum((Z_b - V)**2*M)/np.sum(M))
                        items['Prediction error'] = '{}'.format(error)

                        # Gradient norms
                        if len(grad_norms_policy) > 0:
                            if DEBUG:
                                items['|grad| (policy)']   = [len(grad_norms_policy)] + [f(grad_norms_policy)
                                                              for f in [np.min, np.max, np.mean]]
                                items['|grad| (baseline)'] = [len(grad_norms_baseline)] + [f(grad_norms_baseline)
                                                              for f in [np.min, np.max, np.mean]]
                            grad_norms_policy   = []
                            grad_norms_baseline = []

                        # Print
                        utils.print_dict(items)

                        # Target reward reached
                        if best_reward >= self.config['target_reward']:
                            print("Target reward reached.")
                            return

                        # Terminate
                        if hasattr(self.task, 'terminate'):
                            if self.task.terminate(best_perf):
                                print("Termination criterion satisfied.")
                                return
                    else:
                        #-----------------------------------------------------------------
                        # Ongoing learning
                        #-----------------------------------------------------------------

                        if not training_history:
                            training_history.append(perf)
                        if training_history[0] is None:
                            training_history[0] = perf

                        # Save
                        save = {
                            'iter':                    iter,
                            'config':                  self.config,
                            'policy_config':           self.policy_net.config,
                            'baseline_config':         self.baseline_net.config,
                            'current_policy_params':   self.policy_net.get_values(),
                            'current_baseline_params': self.baseline_net.get_values(),
                            'rng_state':               self.rng.get_state(),
                            'init':                    init,
                            'init_b':                  init_b,
                            'perf':                    perf,
                            'training_history':        training_history,
                            'trials_tot':              trials_tot,
                            'net_sgd':                 self.policy_sgd.get_values(),
                            'baseline_sgd':            self.baseline_sgd.get_values()
                            }
                        utils.save(savefile, save)

                        # Report
                        if iter % 100 == 1:
                            elapsed = utils.elapsed_time(tstart)
                            print("After {} iterations ({})".format(iter, elapsed))
                            if perf is not None:
                                perf.display()

                #-------------------------------------------------------------------------
                # Run trials
                #-------------------------------------------------------------------------

                # Trials
                trials = [self.task.get_condition(self.rng, self.dt)
                          for i in xrange(n_gradient)]

                # Run trials
                (U, Q, Q_b, Z, Z_b, A, R, M, init, init_b, x0, x0_b,
                 perf, r_policy, r_value) = self.run_trials(trials,
                                                            init=init, init_b=init_b,
                                                            return_states=True, perf=perf)

                #-------------------------------------------------------------------------
                # Update baseline
                #-------------------------------------------------------------------------

                baseline_inputs = np.concatenate((r_policy, A), axis=-1)

                # Compute return
                R_b = np.zeros_like(R)
                for k in xrange(R.shape[0]):
                    R_b[k] = np.sum(R[k:]*M[k:], axis=0)

                if use_x0:
                    args = [x0_b]
                else:
                    args = []
                args += [baseline_inputs[:-1], Q_b, R_b, M, baseline_lr]
                b, norm_b = update_baseline(*args)

                norm_b = float(norm_b)
                if np.isfinite(norm_b):
                    grad_norms_baseline.append(float(norm_b))

                #-------------------------------------------------------------------------
                # Update parameters
                #-------------------------------------------------------------------------

                if use_x0:
                    args = [x0]
                else:
                    args = []
                args += [U[:-1], Q, A, R, b, M, lr]
                norm = update_policy(*args)

                norm = float(norm)
                if np.isfinite(norm):
                    grad_norms_policy.append(norm)

                trials_tot += n_gradient

                #-------------------------------------------------------------------------

            print("Reached maximum number of iterations ({}).".format(max_iter))
        except KeyboardInterrupt:
            print("Training interrupted by user during iteration {}.".format(iter))
            sys.exit()