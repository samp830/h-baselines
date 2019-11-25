"""TD3-compatible policies."""
import tensorflow as tf
import tensorflow.contrib.slim as slim
import numpy as np
from functools import reduce

from hbaselines.goal_conditioned.tf_util import get_trainable_vars
from hbaselines.goal_conditioned.tf_util import get_target_updates
from hbaselines.goal_conditioned.tf_util import reduce_std
from hbaselines.goal_conditioned.replay_buffer import ReplayBuffer
from hbaselines.goal_conditioned.replay_buffer import HierReplayBuffer
from hbaselines.utils.reward_fns import negative_distance
from hbaselines.utils.misc import get_manager_ac_space, get_state_indices


# TODO: add as input
FINGERPRINT_DIM = 2


class ActorCriticPolicy(object):
    """Base Actor Critic Policy.

    Attributes
    ----------
    sess : tf.compat.v1.Session
        the current TensorFlow session
    ob_space : gym.spaces.*
        the observation space of the environment
    ac_space : gym.spaces.*
        the action space of the environment
    co_space : gym.spaces.*
        the context space of the environment
    """

    def __init__(self, sess, ob_space, ac_space, co_space):
        """Instantiate the base policy object.

        Parameters
        ----------
        sess : tf.compat.v1.Session
            the current TensorFlow session
        ob_space : gym.spaces.*
            the observation space of the environment
        ac_space : gym.spaces.*
            the action space of the environment
        co_space : gym.spaces.*
            the context space of the environment
        """
        self.sess = sess
        self.ob_space = ob_space
        self.ac_space = ac_space
        self.co_space = co_space

    def initialize(self):
        """Initialize the policy.

        This is used at the beginning of training by the algorithm, after the
        model parameters have been initialized.
        """
        raise NotImplementedError

    def update(self, update_actor=True, **kwargs):
        """Perform a gradient update step.

        Parameters
        ----------
        update_actor : bool
            specifies whether to update the actor policy. The critic policy is
            still updated if this value is set to False.

        Returns
        -------
        float
            critic loss
        float
            actor loss
        """
        raise NotImplementedError

    def get_action(self, obs, context, apply_noise, random_actions):
        """Call the actor methods to compute policy actions.

        Parameters
        ----------
        obs : array_like
            the observation
        context : array_like or None
            the contextual term. Set to None if no context is provided by the
            environment.
        apply_noise : bool
            whether to add Gaussian noise to the output of the actor. Defaults
            to False
        random_actions : bool
            if set to True, actions are sampled randomly from the action space
            instead of being computed by the policy. This is used for
            exploration purposes.

        Returns
        -------
        array_like
            computed action by the policy
        """
        raise NotImplementedError

    def value(self, obs, context, action):
        """Call the critic methods to compute the value.

        Parameters
        ----------
        obs : array_like
            the observation
        context : array_like or None
            the contextual term. Set to None if no context is provided by the
            environment.
        action : array_like
            the actions performed in the given observation

        Returns
        -------
        array_like
            computed value by the critic
        """
        raise NotImplementedError

    def store_transition(self, obs0, context0, action, reward, obs1, context1,
                         done, evaluate=False):
        """Store a transition in the replay buffer.

        Parameters
        ----------
        obs0 : array_like
            the last observation
        context0 : array_like or None
            the last contextual term. Set to None if no context is provided by
            the environment.
        action : array_like
            the action
        reward : float
            the reward
        obs1 : array_like
            the current observation
        context1 : array_like or None
            the current contextual term. Set to None if no context is provided
            by the environment.
        done : float
            is the episode done
        evaluate : bool
            whether the sample is being provided by the evaluation environment.
            If so, the data is not stored in the replay buffer.
        """
        raise NotImplementedError

    def get_td_map(self):
        """Return dict map for the summary (to be run in the algorithm)."""
        raise NotImplementedError

    @staticmethod
    def _get_obs(obs, context, axis=0):
        """Return the processed observation.

        If the contextual term is not None, this will look as follows:

                                    -----------------
                    processed_obs = | obs | context |
                                    -----------------

        Otherwise, this method simply returns the observation.

        Parameters
        ----------
        obs : array_like
            the original observation
        context : array_like or None
            the contextual term. Set to None if no context is provided by the
            environment.
        axis : int
            the axis to concatenate the observations and contextual terms by

        Returns
        -------
        array_like
            the processed observation
        """
        if context is not None:
            context = context.flatten() if axis == 0 else context
            obs = np.concatenate((obs, context), axis=axis)
        return obs


class FeedForwardPolicy(ActorCriticPolicy):
    """Feed-forward neural network actor-critic policy.

    Attributes
    ----------
    sess : tf.compat.v1.Session
        the current TensorFlow session
    ob_space : gym.spaces.*
        the observation space of the environment
    ac_space : gym.spaces.*
        the action space of the environment
    co_space : gym.spaces.*
        the context space of the environment
    buffer_size : int
        the max number of transitions to store
    batch_size : int
        SGD batch size
    actor_lr : float
        actor learning rate
    critic_lr : float
        critic learning rate
    verbose : int
        the verbosity level: 0 none, 1 training information, 2 tensorflow debug
    reuse : bool
        if the policy is reusable or not
    layers : list of int
        the size of the Neural network for the policy
    tau : float
        target update rate
    gamma : float
        discount factor
    noise : float
        scaling term to the range of the action space, that is subsequently
        used as the standard deviation of Gaussian noise added to the action if
        `apply_noise` is set to True in `get_action`
    target_policy_noise : float
        standard deviation term to the noise from the output of the target
        actor policy. See TD3 paper for more.
    target_noise_clip : float
        clipping term for the noise injected in the target actor policy
    layer_norm : bool
        enable layer normalisation
    act_fun : tf.nn.*
        the activation function to use in the neural network
    use_huber : bool
        specifies whether to use the huber distance function as the loss for
        the critic. If set to False, the mean-squared error metric is used
        instead
    zero_fingerprint : bool
        whether to zero the last two elements of the observations for the actor
        and worker computations. Used for the worker policy when fingerprints
        are being implemented.
    replay_buffer : hbaselines.goal_conditioned.replay_buffer.ReplayBuffer
        the replay buffer
    critic_target : tf.compat.v1.placeholder
        a placeholder for the current-step estimate of the target Q values
    terminals1 : tf.compat.v1.placeholder
        placeholder for the next step terminals
    rew_ph : tf.compat.v1.placeholder
        placeholder for the rewards
    action_ph : tf.compat.v1.placeholder
        placeholder for the actions
    obs_ph : tf.compat.v1.placeholder
        placeholder for the observations
    obs1_ph : tf.compat.v1.placeholder
        placeholder for the next step observations
    actor_tf : tf.Variable
        the output from the actor network
    critic_tf : tf.Variable
        the output from the critic network
    critic_with_actor_tf : tf.Variable
        the output from the critic network with the action provided directly by
        the actor policy
    target_q : tf.Variable
        the Q-value as estimated by the current reward and next step estimate
        by the target Q-value
    target_init_updates : tf.Operation
        an operation that sets the values of the trainable parameters of the
        target actor/critic to match those actual actor/critic
    target_soft_updates : tf.Operation
        soft target update function
    actor_loss : tf.Operation
        the operation that returns the loss of the actor
    actor_optimizer : tf.Operation
        the operation that updates the trainable parameters of the actor
    critic_loss : tf.Operation
        the operation that returns the loss of the critic
    critic_optimizer : tf.Operation
        the operation that updates the trainable parameters of the critic
    """

    def __init__(self,
                 sess,
                 ob_space,
                 ac_space,
                 co_space,
                 buffer_size,
                 batch_size,
                 actor_lr,
                 critic_lr,
                 verbose,
                 tau,
                 gamma,
                 noise,
                 target_policy_noise,
                 target_noise_clip,
                 layer_norm,
                 layers,
                 act_fun,
                 use_huber,
                 reuse=False,
                 scope=None,
                 use_fingerprints=False,
                 zero_fingerprint=False):
        """Instantiate the feed-forward neural network policy.

        Parameters
        ----------
        sess : tf.compat.v1.Session
            the current TensorFlow session
        ob_space : gym.spaces.*
            the observation space of the environment
        ac_space : gym.spaces.*
            the action space of the environment
        co_space : gym.spaces.*
            the context space of the environment
        buffer_size : int
            the max number of transitions to store
        batch_size : int
            SGD batch size
        actor_lr : float
            actor learning rate
        critic_lr : float
            critic learning rate
        verbose : int
            the verbosity level: 0 none, 1 training information, 2 tensorflow
            debug
        tau : float
            target update rate
        gamma : float
            discount factor
        noise : float
            scaling term to the range of the action space, that is subsequently
            used as the standard deviation of Gaussian noise added to the
            action if `apply_noise` is set to True in `get_action`
        target_policy_noise : float
            standard deviation term to the noise from the output of the target
            actor policy. See TD3 paper for more.
        target_noise_clip : float
            clipping term for the noise injected in the target actor policy
        layer_norm : bool
            enable layer normalisation
        layers : list of int or None
            the size of the Neural network for the policy
        act_fun : tf.nn.*
            the activation function to use in the neural network
        use_huber : bool
            specifies whether to use the huber distance function as the loss
            for the critic. If set to False, the mean-squared error metric is
            used instead
        reuse : bool
            if the policy is reusable or not
        scope : str
            an upper-level scope term. Used by policies that call this one.

        Raises
        ------
        AssertionError
            if the layers is not a list of at least size 1
        """
        super(FeedForwardPolicy, self).__init__(sess,
                                                ob_space, ac_space, co_space)

        # action magnitudes
        ac_mag = 0.5 * (ac_space.high - ac_space.low)

        self.buffer_size = buffer_size
        self.batch_size = batch_size
        self.actor_lr = actor_lr
        self.critic_lr = critic_lr
        self.verbose = verbose
        self.reuse = reuse
        self.layers = layers
        self.tau = tau
        self.gamma = gamma
        self.noise = noise * ac_mag
        self.target_policy_noise = np.array([ac_mag * target_policy_noise])
        self.target_noise_clip = np.array([ac_mag * target_noise_clip])
        self.layer_norm = layer_norm
        self.act_fun = act_fun
        self.use_huber = use_huber
        self.use_fingerprints = use_fingerprints
        self.zero_fingerprint = zero_fingerprint
        assert len(self.layers) >= 1, \
            "Error: must have at least one hidden layer for the policy."

        # Compute the shape of the input observation space, which may include
        # the contextual term.
        ob_dim = ob_space.shape
        if co_space is not None:
            ob_dim = tuple(map(sum, zip(ob_dim, co_space.shape)))

        # =================================================================== #
        # Step 1: Create a replay buffer object.                              #
        # =================================================================== #

        self.replay_buffer = ReplayBuffer(
            buffer_size=self.buffer_size,
            batch_size=self.batch_size,
            obs_dim=ob_dim[0],
            ac_dim=self.ac_space.shape[0],
        )

        # =================================================================== #
        # Step 2: Create input variables.                                     #
        # =================================================================== #

        with tf.compat.v1.variable_scope("input", reuse=False):
            self.critic_target = tf.compat.v1.placeholder(
                tf.float32,
                shape=(None, 1),
                name='critic_target')
            self.terminals1 = tf.compat.v1.placeholder(
                tf.float32,
                shape=(None, 1),
                name='terminals1')
            self.rew_ph = tf.compat.v1.placeholder(
                tf.float32,
                shape=(None, 1),
                name='rewards')
            self.action_ph = tf.compat.v1.placeholder(
                tf.float32,
                shape=(None,) + ac_space.shape,
                name='actions')
            self.obs_ph = tf.compat.v1.placeholder(
                tf.float32,
                shape=(None,) + ob_dim,
                name='observations')
            self.obs1_ph = tf.compat.v1.placeholder(
                tf.float32,
                shape=(None,) + ob_dim,
                name='observations')

        # logging of rewards to tensorboard
        with tf.compat.v1.variable_scope("input_info", reuse=False):
            tf.compat.v1.summary.scalar('rewards', tf.reduce_mean(self.rew_ph))

        # =================================================================== #
        # Step 3: Create actor and critic variables.                          #
        # =================================================================== #

        # Create networks and core TF parts that are shared across setup parts.
        with tf.compat.v1.variable_scope("model", reuse=False):
            self.actor_tf = self.make_actor(self.obs_ph)
            self.critic_tf = [
                self.make_critic(self.obs_ph, self.action_ph,
                                 scope="qf_{}".format(i))
                for i in range(2)
            ]
            self.critic_with_actor_tf = [
                self.make_critic(self.obs_ph, self.actor_tf, reuse=True,
                                 scope="qf_{}".format(i))
                for i in range(2)
            ]

        with tf.compat.v1.variable_scope("target", reuse=False):
            # create the target actor policy
            actor_target = self.make_actor(self.obs1_ph)

            # smooth target policy by adding clipped noise to target actions
            target_noise = tf.random_normal(
                tf.shape(actor_target), stddev=self.target_policy_noise)
            target_noise = tf.clip_by_value(
                target_noise, -self.target_noise_clip, self.target_noise_clip)

            # clip the noisy action to remain in the bounds
            noisy_actor_target = tf.clip_by_value(
                actor_target + target_noise,
                self.ac_space.low,
                self.ac_space.high
            )

            # create the target critic policies
            critic_target = [
                self.make_critic(self.obs1_ph, noisy_actor_target,
                                 scope="qf_{}".format(i))
                for i in range(2)
            ]

        with tf.compat.v1.variable_scope("loss", reuse=False):
            q_obs1 = tf.minimum(critic_target[0], critic_target[1])
            self.target_q = tf.stop_gradient(
                self.rew_ph + (1. - self.terminals1) * gamma * q_obs1)

            tf.compat.v1.summary.scalar('critic_target',
                                        tf.reduce_mean(self.target_q))

        # Create the target update operations.
        model_scope = 'model/'
        target_scope = 'target/'
        if scope is not None:
            model_scope = scope + '/' + model_scope
            target_scope = scope + '/' + target_scope
        init_updates, soft_updates = get_target_updates(
            get_trainable_vars(model_scope),
            get_trainable_vars(target_scope),
            tau, verbose)
        self.target_init_updates = init_updates
        self.target_soft_updates = soft_updates

        # =================================================================== #
        # Step 4: Setup the optimizers for the actor and critic.              #
        # =================================================================== #

        with tf.compat.v1.variable_scope("Optimizer", reuse=False):
            self._setup_actor_optimizer(scope=scope)
            self._setup_critic_optimizer(scope=scope)
            tf.compat.v1.summary.scalar('actor_loss', self.actor_loss)
            tf.compat.v1.summary.scalar('critic_loss', self.critic_loss)

        # =================================================================== #
        # Step 5: Setup the operations for computing model statistics.        #
        # =================================================================== #

        # Setup the running means and standard deviations of the model inputs
        # and outputs.
        self.stats_ops, self.stats_names = self._setup_stats(scope or "Model")

    def _setup_actor_optimizer(self, scope):
        """Create the actor loss, gradient, and optimizer."""
        if self.verbose >= 2:
            print('setting up actor optimizer')

        scope_name = 'model/pi/'
        if scope is not None:
            scope_name = scope + '/' + scope_name

        # compute the actor loss
        self.actor_loss = -tf.reduce_mean(self.critic_with_actor_tf[0])

        if self.verbose >= 2:
            actor_shapes = [var.get_shape().as_list()
                            for var in get_trainable_vars(scope_name)]
            actor_nb_params = sum([reduce(lambda x, y: x * y, shape)
                                   for shape in actor_shapes])
            print('  actor shapes: {}'.format(actor_shapes))
            print('  actor params: {}'.format(actor_nb_params))

        # create an optimizer object
        optimizer = tf.compat.v1.train.AdamOptimizer(self.actor_lr)

        self.actor_optimizer = optimizer.minimize(
            self.actor_loss,
            var_list=get_trainable_vars(scope_name)
        )

    def _setup_critic_optimizer(self, scope):
        """Create the critic loss, gradient, and optimizer."""
        if self.verbose >= 2:
            print('setting up critic optimizer')

        # choose the loss function
        if self.use_huber:
            loss_fn = tf.compat.v1.losses.huber_loss
        else:
            loss_fn = tf.compat.v1.losses.mean_squared_error

        self.critic_loss = \
            loss_fn(self.critic_tf[0], self.target_q) + \
            loss_fn(self.critic_tf[1], self.target_q)

        self.critic_optimizer = []

        for i in range(2):
            scope_name = 'model/qf_{}/'.format(i)
            if scope is not None:
                scope_name = scope + '/' + scope_name

            if self.verbose >= 2:
                critic_shapes = [var.get_shape().as_list()
                                 for var in get_trainable_vars(scope_name)]
                critic_nb_params = sum([reduce(lambda x, y: x * y, shape)
                                        for shape in critic_shapes])
                print('  critic shapes: {}'.format(critic_shapes))
                print('  critic params: {}'.format(critic_nb_params))

            # create an optimizer object
            optimizer = tf.compat.v1.train.AdamOptimizer(self.critic_lr)

            # create the optimizer object
            self.critic_optimizer.append(optimizer.minimize(self.critic_loss))

    def make_actor(self, obs, reuse=False, scope="pi"):
        """Create an actor tensor.

        Parameters
        ----------
        obs : tf.compat.v1.placeholder
            the input observation placeholder
        reuse : bool
            whether or not to reuse parameters
        scope : str
            the scope name of the actor

        Returns
        -------
        tf.Variable
            the output from the actor
        """
        with tf.compat.v1.variable_scope(scope, reuse=reuse):
            pi_h = obs

            # zero out the fingerprint observations for the worker policy
            if self.zero_fingerprint:
                ob_dim = self.ob_space.shape[0]
                co_dim = self.co_space.shape[0]
                pi_h *= tf.constant([1.0] * (ob_dim - FINGERPRINT_DIM)
                                    + [0.0] * FINGERPRINT_DIM
                                    + [1.0] * co_dim)

            # create the hidden layers
            for i, layer_size in enumerate(self.layers):
                pi_h = tf.layers.dense(
                    pi_h,
                    layer_size,
                    name='fc' + str(i),
                    kernel_initializer=slim.variance_scaling_initializer(
                        factor=1.0 / 3.0, mode='FAN_IN', uniform=True))
                if self.layer_norm:
                    pi_h = tf.contrib.layers.layer_norm(
                        pi_h, center=True, scale=True)
                pi_h = self.act_fun(pi_h)

            # create the output layer
            policy = tf.nn.tanh(tf.layers.dense(
                pi_h,
                self.ac_space.shape[0],
                name='output',
                kernel_initializer=tf.random_uniform_initializer(
                    minval=-3e-3, maxval=3e-3)))

            # scaling terms to the output from the policy
            ac_means = (self.ac_space.high + self.ac_space.low) / 2.
            ac_magnitudes = (self.ac_space.high - self.ac_space.low) / 2.

            policy = ac_means + ac_magnitudes * policy

        return policy

    def make_critic(self, obs, action, reuse=False, scope="qf"):
        """Create a critic tensor.

        Parameters
        ----------
        obs : tf.compat.v1.placeholder
            the input observation placeholder
        action : tf.compat.v1.placeholder
            the input action placeholder
        reuse : bool
            whether or not to reuse parameters
        scope : str
            the scope name of the actor

        Returns
        -------
        tf.Variable
            the output from the critic
        """
        with tf.compat.v1.variable_scope(scope, reuse=reuse):
            # concatenate the observations and actions
            qf_h = tf.concat([obs, action], axis=-1)

            # zero out the fingerprint observations for the worker policy
            if self.zero_fingerprint:
                ob_dim = self.ob_space.shape[0]
                co_dim = self.co_space.shape[0]
                ac_dim = self.ac_space.shape[0]
                qf_h *= tf.constant([1.0] * (ob_dim - FINGERPRINT_DIM)
                                    + [0.0] * FINGERPRINT_DIM
                                    + [1.0] * (co_dim + ac_dim))

            # create the hidden layers
            for i, layer_size in enumerate(self.layers):
                qf_h = tf.layers.dense(
                    qf_h,
                    layer_size,
                    name='fc' + str(i),
                    kernel_initializer=slim.variance_scaling_initializer(
                        factor=1.0 / 3.0, mode='FAN_IN', uniform=True))
                if self.layer_norm:
                    qf_h = tf.contrib.layers.layer_norm(
                        qf_h, center=True, scale=True)
                qf_h = self.act_fun(qf_h)

            # create the output layer
            qvalue_fn = tf.layers.dense(
                qf_h,
                1,
                name='qf_output',
                kernel_initializer=tf.random_uniform_initializer(
                    minval=-3e-3, maxval=3e-3))

        return qvalue_fn

    def update(self, update_actor=True, **kwargs):
        """Perform a gradient update step.

        **Note**; The target update soft updates occur at the same frequency as
        the actor update frequencies.

        Parameters
        ----------
        update_actor : bool
            specifies whether to update the actor policy. The critic policy is
            still updated if this value is set to False.

        Returns
        -------
        float
            critic loss
        float
            actor loss
        """
        # Not enough samples in the replay buffer.
        if not self.replay_buffer.can_sample():
            return 0, 0

        # Get a batch
        obs0, actions, rewards, obs1, terminals1 = self.replay_buffer.sample()

        return self.update_from_batch(obs0, actions, rewards, obs1, terminals1,
                                      update_actor=update_actor)

    def update_from_batch(self,
                          obs0,
                          actions,
                          rewards,
                          obs1,
                          terminals1,
                          update_actor=True):
        """Perform gradient update step given a batch of data.

        Parameters
        ----------
        obs0 : np.ndarray
            batch of observations
        actions : numpy float
            batch of actions executed given obs_batch
        rewards : numpy float
            rewards received as results of executing act_batch
        obs1 : np.ndarray
            next set of observations seen after executing act_batch
        terminals1 : numpy bool
            done_mask[i] = 1 if executing act_batch[i] resulted in the end of
            an episode and 0 otherwise.
        update_actor : bool, optional
            specified whether to perform gradient update procedures to the
            actor policy. Default set to True. Note that the update procedure
            for the critic is always performed when calling this method.

        Returns
        -------
        float
            critic loss
        float
            actor loss
        """
        # Reshape to match previous behavior and placeholder shape.
        rewards = rewards.reshape(-1, 1)
        terminals1 = terminals1.reshape(-1, 1)

        # Update operations for the critic networks.
        step_ops = [self.critic_loss,
                    self.critic_optimizer[0],
                    self.critic_optimizer[1]]

        if update_actor:
            # Actor updates and target soft update operation.
            step_ops += [self.actor_loss,
                         self.actor_optimizer,
                         self.target_soft_updates]

        # Perform the update operations and collect the critic loss.
        critic_loss, *_vals = self.sess.run(step_ops, feed_dict={
            self.obs_ph: obs0,
            self.action_ph: actions,
            self.rew_ph: rewards,
            self.obs1_ph: obs1,
            self.terminals1: terminals1
        })

        # Extract the actor loss.
        actor_loss = _vals[2] if update_actor else 0

        return critic_loss, actor_loss

    def get_action(self, obs, context, apply_noise, random_actions):
        """See parent class."""
        # Add the contextual observation, if applicable.
        obs = self._get_obs(obs, context, axis=1)

        if random_actions:
            action = np.array([self.ac_space.sample()])
        else:
            action = self.sess.run(self.actor_tf, {self.obs_ph: obs})

            if apply_noise:
                # compute noisy action
                if apply_noise:
                    action += np.random.normal(0, self.noise, action.shape)

                # clip by bounds
                action = np.clip(action, self.ac_space.low, self.ac_space.high)

        return action

    def value(self, obs, context, action):
        """See parent class."""
        # Add the contextual observation, if applicable.
        obs = self._get_obs(obs, context, axis=1)

        return self.sess.run(
            self.critic_tf,
            feed_dict={self.obs_ph: obs, self.action_ph: action})

    def store_transition(self, obs0, context0, action, reward, obs1, context1,
                         done, evaluate=False):
        """See parent class."""
        if not evaluate:
            # Add the contextual observation, if applicable.
            obs0 = self._get_obs(obs0, context0, axis=0)
            obs1 = self._get_obs(obs1, context1, axis=0)

            self.replay_buffer.add(obs0, action, reward, obs1, float(done))

    def initialize(self):
        """See parent class.

        This method syncs the actor and critic optimizers across CPUs, and
        initializes the target parameters to match the model parameters.
        """
        self.sess.run(self.target_init_updates)

    def _setup_stats(self, base="Model"):
        """Create the running means and std of the model inputs and outputs.

        This method also adds the same running means and stds as scalars to
        tensorboard for additional storage.
        """
        ops = []
        names = []

        ops += [tf.reduce_mean(self.critic_tf[0])]
        names += ['{}/reference_Q1_mean'.format(base)]
        ops += [reduce_std(self.critic_tf[0])]
        names += ['{}/reference_Q1_std'.format(base)]

        ops += [tf.reduce_mean(self.critic_tf[1])]
        names += ['{}/reference_Q2_mean'.format(base)]
        ops += [reduce_std(self.critic_tf[1])]
        names += ['{}/reference_Q2_std'.format(base)]

        ops += [tf.reduce_mean(self.critic_with_actor_tf[0])]
        names += ['{}/reference_actor_Q1_mean'.format(base)]
        ops += [reduce_std(self.critic_with_actor_tf[0])]
        names += ['{}/reference_actor_Q1_std'.format(base)]

        ops += [tf.reduce_mean(self.critic_with_actor_tf[1])]
        names += ['{}/reference_actor_Q2_mean'.format(base)]
        ops += [reduce_std(self.critic_with_actor_tf[1])]
        names += ['{}/reference_actor_Q2_std'.format(base)]

        ops += [tf.reduce_mean(self.actor_tf)]
        names += ['{}/reference_action_mean'.format(base)]
        ops += [reduce_std(self.actor_tf)]
        names += ['{}/reference_action_std'.format(base)]

        # Add all names and ops to the tensorboard summary.
        for op, name in zip(ops, names):
            tf.compat.v1.summary.scalar(name, op)

        return ops, names

    def get_td_map(self):
        """See parent class."""
        # Not enough samples in the replay buffer.
        if not self.replay_buffer.can_sample():
            return {}

        # Get a batch.
        obs0, actions, rewards, obs1, terminals1 = self.replay_buffer.sample()

        return self.get_td_map_from_batch(
            obs0, actions, rewards, obs1, terminals1)

    def get_td_map_from_batch(self, obs0, actions, rewards, obs1, terminals1):
        """Convert a batch to a td_map."""
        # Reshape to match previous behavior and placeholder shape.
        rewards = rewards.reshape(-1, 1)
        terminals1 = terminals1.reshape(-1, 1)

        td_map = {
            self.obs_ph: obs0,
            self.action_ph: actions,
            self.rew_ph: rewards,
            self.obs1_ph: obs1,
            self.terminals1: terminals1
        }

        return td_map


class GoalConditionedPolicy(ActorCriticPolicy):
    """Goal-conditioned hierarchical reinforcement learning model.

    This policy is an implementation of the two-level hierarchy presented
    in [1], which itself is similar to the feudal networks formulation [2, 3].
    This network consists of a high-level, or Manager, pi_{\theta_H} that
    computes and outputs goals g_t ~ pi_{\theta_H}(s_t, h) every `meta_period`
    time steps, and a low-level policy pi_{\theta_L} that takes as inputs the
    current state and the assigned goals and attempts to perform an action
    a_t ~ pi_{\theta_L}(s_t,g_t) that satisfies these goals.

    The Manager is rewarded based on the original environment reward function:
    r_H = r(s,a;h).

    The Target term, h, parameterizes the reward assigned to the Manager in
    order to allow the policy to generalize to several goals within a task, a
    technique that was first proposed by [4].

    Finally, the Worker is motivated to follow the goals set by the Manager via
    an intrinsic reward based on the distance between the current observation
    and the goal observation:
    r_L (s_t, g_t, s_{t+1}) = -||s_t + g_t - s_{t+1}||_2

    Bibliography:

    [1] Nachum, Ofir, et al. "Data-efficient hierarchical reinforcement
        learning." Advances in Neural Information Processing Systems. 2018.
    [2] Dayan, Peter, and Geoffrey E. Hinton. "Feudal reinforcement learning."
        Advances in neural information processing systems. 1993.
    [3] Vezhnevets, Alexander Sasha, et al. "Feudal networks for hierarchical
        reinforcement learning." Proceedings of the 34th International
        Conference on Machine Learning-Volume 70. JMLR. org, 2017.
    [4] Schaul, Tom, et al. "Universal value function approximators."
        International Conference on Machine Learning. 2015.

    Attributes
    ----------
    manager : hbaselines.goal_conditioned.policy.FeedForwardPolicy
        the manager policy
    meta_period : int
        manger action period
    worker_reward_scale : float
        the value the intrinsic (Worker) reward should be scaled by
    relative_goals : bool
        specifies whether the goal issued by the Manager is meant to be a
        relative or absolute goal, i.e. specific state or change in state
    off_policy_corrections : bool
        whether to use off-policy corrections during the update procedure. See:
        https://arxiv.org/abs/1805.08296.
    use_fingerprints : bool
        specifies whether to add a time-dependent fingerprint to the
        observations
    fingerprint_range : (list of float, list of float)
        the low and high values for each fingerprint element, if they are being
        used
    fingerprint_dim : tuple of int
        the shape of the fingerprint elements, if they are being used
    centralized_value_functions : bool
        specifies whether to use centralized value functions for the Manager
        critic functions
    connected_gradients : bool
        whether to connect the graph between the manager and worker
    cg_weights : float
        weights for the gradients of the loss of the worker with respect to the
        parameters of the manager. Only used if `connected_gradients` is set to
        True.
    prev_meta_obs : array_like
        previous observation by the Manager
    meta_action : array_like
        current action by the Manager
    meta_reward : float
        current meta reward, counting as the cumulative environment reward
        during the meta period
    batch_size : int
        SGD batch size
    worker : hbaselines.goal_conditioned.policy.FeedForwardPolicy
        the worker policy
    worker_reward_fn : function
        reward function for the worker
    """

    def __init__(self,
                 sess,
                 ob_space,
                 ac_space,
                 co_space,
                 buffer_size,
                 batch_size,
                 actor_lr,
                 critic_lr,
                 verbose,
                 tau,
                 gamma,
                 noise,
                 target_policy_noise,
                 target_noise_clip,
                 layer_norm,
                 layers,
                 act_fun,
                 use_huber,
                 meta_period,
                 worker_reward_scale,
                 relative_goals,
                 off_policy_corrections,
                 use_fingerprints,
                 fingerprint_range,
                 centralized_value_functions,
                 connected_gradients,
                 cg_weights,
                 reuse=False,
                 env_name=""):
        """Instantiate the goal-conditioned hierarchical policy.

        Parameters
        ----------
        sess : tf.compat.v1.Session
            the current TensorFlow session
        ob_space : gym.spaces.*
            the observation space of the environment
        ac_space : gym.spaces.*
            the action space of the environment
        co_space : gym.spaces.*
            the context space of the environment
        buffer_size : int
            the max number of transitions to store
        batch_size : int
            SGD batch size
        actor_lr : float
            actor learning rate
        critic_lr : float
            critic learning rate
        verbose : int
            the verbosity level: 0 none, 1 training information, 2 tensorflow
            debug
        tau : float
            target update rate
        gamma : float
            discount factor
        noise : float
            scaling term to the range of the action space, that is subsequently
            used as the standard deviation of Gaussian noise added to the
            action if `apply_noise` is set to True in `get_action`.
        target_policy_noise : float
            standard deviation term to the noise from the output of the target
            actor policy. See TD3 paper for more.
        target_noise_clip : float
            clipping term for the noise injected in the target actor policy
        layer_norm : bool
            enable layer normalisation
        reuse : bool
            if the policy is reusable or not
        layers : list of int or None
            the size of the neural network for the policy
        act_fun : tf.nn.*
            the activation function to use in the neural network
        use_huber : bool
            specifies whether to use the huber distance function as the loss
            for the critic. If set to False, the mean-squared error metric is
            used instead
        meta_period : int
            manger action period
        worker_reward_scale : float
            the value the intrinsic (Worker) reward should be scaled by
        relative_goals : bool
            specifies whether the goal issued by the Manager is meant to be a
            relative or absolute goal, i.e. specific state or change in state
        off_policy_corrections : bool
            whether to use off-policy corrections during the update procedure.
            See: https://arxiv.org/abs/1805.08296
        use_fingerprints : bool
            specifies whether to add a time-dependent fingerprint to the
            observations
        fingerprint_range : (list of float, list of float)
            the low and high values for each fingerprint element, if they are
            being used
        centralized_value_functions : bool
            specifies whether to use centralized value functions for the
            Manager and Worker critic functions
        connected_gradients : bool
            whether to connect the graph between the manager and worker
        cg_weights : float
            weights for the gradients of the loss of the worker with respect to
            the parameters of the manager. Only used if `connected_gradients`
            is set to True.
        """
        super(GoalConditionedPolicy, self).__init__(
            sess, ob_space, ac_space, co_space)

        self.meta_period = meta_period
        self.worker_reward_scale = worker_reward_scale
        self.relative_goals = relative_goals
        self.off_policy_corrections = off_policy_corrections
        self.use_fingerprints = use_fingerprints
        self.fingerprint_range = fingerprint_range
        self.fingerprint_dim = (len(self.fingerprint_range[0]),)
        self.centralized_value_functions = centralized_value_functions
        self.connected_gradients = connected_gradients
        self.cg_weights = cg_weights

        # Get the Manager's action space.
        manager_ac_space = get_manager_ac_space(
            ob_space, relative_goals, env_name,
            use_fingerprints, self.fingerprint_dim)

        # Manager observation size
        meta_ob_dim = ob_space.shape[0]
        if co_space is not None:
            meta_ob_dim += co_space.shape[0]

        # Create the replay buffer.
        self.replay_buffer = HierReplayBuffer(
            buffer_size=int(buffer_size/meta_period),
            batch_size=batch_size,
            meta_obs_dim=meta_ob_dim,
            meta_ac_dim=manager_ac_space.shape[0],
            worker_obs_dim=ob_space.shape[0] + manager_ac_space.shape[0],
            worker_ac_dim=ac_space.shape[0],
        )

        # =================================================================== #
        # Part 1. Setup the Manager                                           #
        # =================================================================== #

        # Create the Manager policy.
        with tf.compat.v1.variable_scope("Manager"):
            self.manager = FeedForwardPolicy(
                sess=sess,
                ob_space=ob_space,
                ac_space=manager_ac_space,
                co_space=co_space,
                buffer_size=buffer_size,
                batch_size=batch_size,
                actor_lr=actor_lr,
                critic_lr=critic_lr,
                verbose=verbose,
                tau=tau,
                gamma=gamma,
                layer_norm=layer_norm,
                reuse=reuse,
                layers=layers,
                act_fun=act_fun,
                use_huber=use_huber,
                scope="Manager",
                noise=noise,
                target_policy_noise=target_policy_noise,
                target_noise_clip=target_noise_clip,
                use_fingerprints=self.use_fingerprints,
                zero_fingerprint=False,
            )

        # a fixed goal transition function for the meta-actions in between meta
        # periods. This is used when relative_goals is set to True in order to
        # maintain a fixed absolute position of the goal.
        if relative_goals:
            def goal_transition_fn(obs0, goal, obs1):
                return obs0 + goal - obs1
        else:
            def goal_transition_fn(obs0, goal, obs1):
                return goal
        self.goal_transition_fn = goal_transition_fn

        # previous observation by the Manager
        self.prev_meta_obs = None

        # current action by the Manager
        self.meta_action = None

        # current meta reward, counting as the cumulative environment reward
        # during the meta period
        self.meta_reward = None

        # The following is redundant but necessary if the changes to the update
        # function are to be in the GoalConditionedPolicy policy and not
        # FeedForwardPolicy.
        self.batch_size = batch_size

        # Use this to store a list of observations that stretch as long as the
        # dilated horizon chosen for the Manager. These observations correspond
        # to the s(t) in the HIRO paper.
        self._observations = []

        # Use this to store the list of environmental actions that the worker
        # takes. These actions correspond to the a(t) in the HIRO paper.
        self._worker_actions = []

        # rewards provided by the policy to the worker
        self._worker_rewards = []

        # done masks at every time step for the worker
        self._dones = []

        # actions performed by the manager during a given meta period. Used by
        # the replay buffer.
        self._meta_actions = []

        # =================================================================== #
        # Part 2. Setup the Worker                                            #
        # =================================================================== #

        # Create the Worker policy.
        with tf.compat.v1.variable_scope("Worker"):
            self.worker = FeedForwardPolicy(
                sess,
                ob_space=ob_space,
                ac_space=ac_space,
                co_space=manager_ac_space,
                buffer_size=buffer_size,
                batch_size=batch_size,
                actor_lr=actor_lr,
                critic_lr=critic_lr,
                verbose=verbose,
                tau=tau,
                gamma=gamma,
                layer_norm=layer_norm,
                reuse=reuse,
                layers=layers,
                act_fun=act_fun,
                use_huber=use_huber,
                scope="Worker",
                noise=noise,
                target_policy_noise=target_policy_noise,
                target_noise_clip=target_noise_clip,
                use_fingerprints=self.use_fingerprints,
                zero_fingerprint=self.use_fingerprints,
            )

        # Collect the state indices for the worker rewards.
        state_indices = get_state_indices(
            ob_space, env_name, use_fingerprints, self.fingerprint_dim)

        # reward function for the worker
        def worker_reward_fn(states, goals, next_states):
            return negative_distance(
                states=states,
                state_indices=state_indices,
                goals=goals,
                next_states=next_states,
                relative_context=relative_goals,
                offset=0.0
            )
        self.worker_reward_fn = worker_reward_fn

        if self.connected_gradients:
            self._setup_connected_gradients()

    def initialize(self):
        """See parent class.

        This method calls the initialization methods of the manager and worker.
        """
        self.manager.initialize()
        self.worker.initialize()
        self.meta_reward = 0

    def update(self, update_actor=True, **kwargs):
        """Perform a gradient update step.

        This is done both at the level of the Manager and Worker policies.

        The kwargs argument for this method contains two additional terms:

        * update_meta (bool): specifies whether to perform a gradient update
          step for the meta-policy (i.e. Manager)
        * update_meta_actor (bool): similar to the `update_policy` term, but
          for the meta-policy. Note that, if `update_meta` is set to False,
          this term is void.

        **Note**; The target update soft updates for both the manager and the
        worker policies occur at the same frequency as their respective actor
        update frequencies.

        Parameters
        ----------
        update_actor : bool
            specifies whether to update the actor policy. The critic policy is
            still updated if this value is set to False.

        Returns
        -------
        (float, float)
            manager critic loss, worker critic loss
        (float, float)
            manager actor loss, worker actor loss
        """
        # Not enough samples in the replay buffer.
        if not self.replay_buffer.can_sample():
            return (0, 0), (0, 0)

        # Get a batch.
        meta_obs0, meta_obs1, meta_act, meta_rew, meta_done, worker_obs0, \
            worker_obs1, worker_act, worker_rew, worker_done = \
            self.replay_buffer.sample()

        # Update the Manager policy.
        if kwargs['update_meta']:
            if self.connected_gradients:
                # Perform the connected gradients update procedure.
                m_critic_loss, m_actor_loss = self._connected_gradients_update(
                    obs0=meta_obs0,
                    actions=meta_act,
                    rewards=meta_rew,
                    obs1=meta_obs1,
                    terminals1=meta_done,
                    update_actor=kwargs['update_meta_actor'],
                    worker_obs0=worker_obs0,
                    worker_obs1=worker_obs1,
                    worker_actions=worker_act,
                )
            else:
                m_critic_loss, m_actor_loss = self.manager.update_from_batch(
                    obs0=meta_obs0,
                    actions=meta_act,
                    rewards=meta_rew,
                    obs1=meta_obs1,
                    terminals1=meta_done,
                    update_actor=kwargs['update_meta_actor'],
                )
        else:
            m_critic_loss, m_actor_loss = 0, 0

        # Update the Worker policy.
        w_critic_loss, w_actor_loss = self.worker.update_from_batch(
            obs0=worker_obs0,
            actions=worker_act,
            rewards=worker_rew,
            obs1=worker_obs1,
            terminals1=worker_done,
            update_actor=update_actor,
        )

        return (m_critic_loss, w_critic_loss), (m_actor_loss, w_actor_loss)

    def get_action(self, obs, context, apply_noise, random_actions):
        """See parent class."""
        if self._update_meta:
            # Update the meta action based on the output from the policy if the
            # time period requires is.
            self.meta_action = self.manager.get_action(
                obs, context, apply_noise, random_actions)
        else:
            # Update the meta-action in accordance with the fixed transition
            # function.
            goal_dim = self.meta_action.shape[1]
            self.meta_action = self.goal_transition_fn(
                obs0=np.asarray([self._observations[-1][:goal_dim]]),
                goal=self.meta_action,
                obs1=obs[:, :goal_dim]
            )

        # Return the worker action.
        worker_action = self.worker.get_action(
            obs, self.meta_action, apply_noise, random_actions)

        return worker_action

    def value(self, obs, context, action):
        """See parent class."""
        return 0, 0  # FIXME

    def store_transition(self, obs0, context0, action, reward, obs1, context1,
                         done, evaluate=False):
        """See parent class."""
        # Compute the worker reward and append it to the list of rewards.
        self._worker_rewards.append(
            self.worker_reward_scale *
            self.worker_reward_fn(obs0, self.meta_action.flatten(), obs1)
        )

        # Add the environmental observations and done masks, and the manager
        # and worker actions to their respective lists.
        self._worker_actions.append(action)
        self._meta_actions.append(self.meta_action.flatten())
        self._observations.append(self._get_obs(obs0, self.meta_action, 0))
        self._dones.append(done)

        # Increment the meta reward with the most recent reward.
        self.meta_reward += reward

        # Modify the previous meta observation whenever the action has changed.
        if len(self._observations) == 1:
            self.prev_meta_obs = self._get_obs(obs0, context0, 0)

        # Add a sample to the replay buffer.
        if len(self._observations) == self.meta_period or done:
            # Add the last observation.
            self._observations.append(self._get_obs(obs1, self.meta_action, 0))

            # Add the contextual observation to the most recent environmental
            # observation, if applicable.
            meta_obs1 = self._get_obs(obs1, context1, 0)

            # Store a sample in the Manager policy.
            if not evaluate:
                self.replay_buffer.add(
                    obs_t=self._observations,
                    goal_t=self._meta_actions[0],
                    action_t=self._worker_actions,
                    reward_t=self._worker_rewards,
                    done=self._dones,
                    meta_obs_t=(self.prev_meta_obs, meta_obs1),
                    meta_reward_t=self.meta_reward,
                )

            # Clear the worker rewards and actions, and the environmental
            # observation and reward.
            self.clear_memory()

    @property
    def _update_meta(self):
        """Return True if the meta-action should be updated by the policy.

        This is done by checking the length of the observation lists that are
        passed to the replay buffer, which are cleared whenever the meta-period
        has been met or the environment has been reset.
        """
        return len(self._observations) == 0

    def clear_memory(self):
        """Clear internal memory that is used by the replay buffer.

        By clearing memory, the Manager policy is then informed during the
        `get_action` procedure to update the meta-action.
        """
        self.meta_reward = 0
        self._observations = []
        self._worker_actions = []
        self._worker_rewards = []
        self._dones = []
        self._meta_actions = []

    def _sample_best_meta_action(self,
                                 state_reps,
                                 next_state_reprs,
                                 prev_meta_actions,
                                 low_states,
                                 low_actions,
                                 low_state_reprs,
                                 k=8):
        """Return meta-actions that approximately maximize low-level log-probs.

        Parameters
        ----------
        state_reps : array_like
            current Manager state observation
        next_state_reprs : array_like
            next Manager state observation
        prev_meta_actions : array_like
            previous meta Manager action
        low_states : array_like
            current Worker state observation
        low_actions : array_like
            current Worker environmental action
        low_state_reprs : array_like
            current Worker state observation
        k : int, optional
            number of goals returned, excluding the initial goal and the mean
            value

        Returns
        -------
        array_like
            most likely meta-actions
        """
        # Collect several samples of potentially optimal goals.
        sampled_actions = self._sample(
            state_reps, next_state_reprs, k, prev_meta_actions)

        sampled_log_probs = tf.reshape(self._log_probs(
            tf.tile(low_states, [k, 1, 1]),
            tf.tile(low_actions, [k, 1, 1]),
            tf.tile(low_state_reprs, [k, 1, 1]),
            [tf.reshape(sampled_actions, [-1, sampled_actions.shape[-1]])]),
            [k, low_states.shape[0], low_states.shape[1], -1])

        fitness = tf.reduce_sum(sampled_log_probs, [2, 3])
        best_actions = tf.argmax(fitness, 0)
        best_goals = tf.gather_nd(
            sampled_actions,
            tf.stack([
                best_actions,
                tf.range(prev_meta_actions.shape[0], dtype=tf.int64)], -1))

        return best_goals

    def _log_probs(self, manager_obs, worker_obs, actions, goals):
        """Calculate the log probability of the next goal by the Manager.

        Parameters
        ----------
        manager_obs : array_like
            (batch_size, m_obs_dim) matrix of manager observations
        worker_obs : array_like
            (batch_size, w_obs_dim, meta_period) matrix of worker observations
        actions : array_like
            (batch_size, ac_dim, meta_period-1) list of low-level actions
        goals : array_like
            (batch_size, goal_dim, num_samples) matrix of sampled goals

        Returns
        -------
        array_like
            (batch_size, num_samples) error associated with every state /
            action / goal pair

        Helps
        -----
        * _sample_best_meta_action(self):
        """
        # Action a policy would perform given a specific observation / goal.
        pred_actions = self.worker.get_action(
            worker_obs, goals,
            apply_noise=False,
            random_actions=False)

        # Normalize the error based on the range of applicable goals.
        goal_space = self.manager.ac_space
        spec_range = goal_space.high - goal_space.low
        scale = np.tile(np.square(spec_range), (manager_obs.shape[0], 1))

        # Compute error as the distance between expected and actual actions.
        normalized_error = np.mean(
            np.square(np.divide(actions - pred_actions, scale)), axis=1)

        return -normalized_error

    def _sample(self, states, next_states, num_samples, orig_goals, sc=0.5):
        """Sample different goals.

        These goals are acquired from a random Gaussian distribution centered
        at s_{t+c} - s_t.

        Parameters
        ----------
        states : array_like
            (batch_size, obs_dim) matrix of current time step observation
        next_states : array_like
            (batch_size, obs_dim) matrix of next time step observation
        num_samples : int
            number of samples
        orig_goals : array_like
            (batch_size, goal_dim) matrix of original goal specified by Manager
        sc : float
            scaling factor for the normal distribution.

        Returns
        -------
        array_like
            (batch_size, goal_dim, num_samples) matrix of sampled goals

        Helps
        -----
        * _sample_best_meta_action(self)
        """
        batch_size, goal_dim = orig_goals.shape
        goal_space = self.manager.ac_space
        spec_range = goal_space.high - goal_space.low

        # Compute the mean and std for the Gaussian distribution to sample from
        loc = np.tile((next_states - states)[:, :goal_dim].flatten(),
                      (num_samples-2, 1))
        scale = np.tile(sc * spec_range / 2, (num_samples-2, batch_size))

        # Sample the requested number of goals from the Gaussian distribution.
        samples = loc + np.random.normal(
            size=(num_samples - 2, goal_dim * batch_size)) * scale

        # Add the original goal and the average of the original and final state
        # to the sampled goals.
        samples = np.vstack(
            [samples,
             (next_states - states)[:, :goal_dim].flatten(),
             orig_goals.flatten()],
        )

        # Clip the values based on the Manager action space range.
        minimum = np.tile(goal_space.low, (num_samples, batch_size))
        maximum = np.tile(goal_space.high, (num_samples, batch_size))
        samples = np.minimum(np.maximum(samples, minimum), maximum)

        # Reshape to (batch_size, goal_dim, num_samples).
        samples = samples.T.reshape((batch_size, goal_dim, num_samples))

        return samples

    def get_td_map(self):
        """See parent class."""
        # Not enough samples in the replay buffer.
        if not self.replay_buffer.can_sample():
            return {}

        # Get a batch.
        meta_obs0, meta_obs1, meta_act, meta_rew, meta_done, worker_obs0, \
            worker_obs1, worker_act, worker_rew, worker_done = \
            self.replay_buffer.sample()

        td_map = {}
        td_map.update(self.manager.get_td_map_from_batch(
            meta_obs0, meta_act, meta_rew, meta_obs1, meta_done))
        td_map.update(self.worker.get_td_map_from_batch(
            worker_obs0, worker_act, worker_rew, worker_obs1, worker_done))

        return td_map

    def _setup_connected_gradients(self):
        """Create the updated manager optimization with connected gradients."""
        goal_dim = self.manager.ac_space.shape[0]

        if self.relative_goals:
            # The observation from the perspective of the manager can be
            # collected from the first goal_dim elements of the observation. We
            # use goal_dim in case the goal-specific observations are not the
            # entire observation space.
            obs_t = self.manager.obs_ph[:, :goal_dim]
            # We collect the observation of the worker in a similar fashion as
            # above.
            obs_tpi = self.worker.obs_ph[:, :goal_dim]
            # Relative goal formulation as per HIRO.
            goal = obs_t + self.manager.actor_tf - obs_tpi
        else:
            # Goal is the direct output from the manager in this case.
            goal = self.manager.actor_tf

        # concatenate the output from the manager with the worker policy.
        obs_shape = self.worker.ob_space.shape[0]
        obs = tf.concat([self.worker.obs_ph[:, :obs_shape], goal], axis=-1)

        # create the worker policy with inputs directly from the manager
        with tf.compat.v1.variable_scope("Worker/model"):
            worker_with_manager_obs = self.worker.make_critic(
                obs, self.worker.action_ph, reuse=True, scope="qf_0")

        # create a tensorflow operation that mimics the reward function that is
        # used to provide feedback to the worker
        if self.relative_goals:
            reward_fn = -tf.compat.v1.losses.mean_squared_error(
                self.worker.obs_ph[:, :goal_dim] + goal,
                self.worker.obs1_ph[:, :goal_dim])
        else:
            reward_fn = -tf.compat.v1.losses.mean_squared_error(
                goal, self.worker.obs1_ph[:, :goal_dim])

        # compute the worker loss with respect to the manager actions
        self.cg_loss = - tf.reduce_mean(worker_with_manager_obs) - reward_fn

        # create the optimizer object
        optimizer = tf.compat.v1.train.AdamOptimizer(self.manager.actor_lr)
        self.cg_optimizer = optimizer.minimize(
            self.manager.actor_loss + self.cg_weights * self.cg_loss,
            var_list=get_trainable_vars("Manager/model/pi/"),
        )

    def _connected_gradients_update(self,
                                    obs0,
                                    actions,
                                    rewards,
                                    obs1,
                                    terminals1,
                                    worker_obs0,
                                    worker_obs1,
                                    worker_actions,
                                    update_actor=True):
        """Perform the gradient update procedure for the HRL-CG algorithm.

        This procedure is similar to self.manager.update_from_batch, expect it
        runs the self.cg_optimizer operation instead of self.manager.optimizer,
        and utilizes some information from the worker samples as well.

        Parameters
        ----------
        obs0 : np.ndarray
            batch of manager observations
        actions : numpy float
            batch of manager actions executed given obs_batch
        rewards : numpy float
            manager rewards received as results of executing act_batch
        obs1 : np.ndarray
            set of next manager observations seen after executing act_batch
        terminals1 : numpy bool
            done_mask[i] = 1 if executing act_batch[i] resulted in the end of
            an episode and 0 otherwise.
        worker_obs0 : array_like
            batch of worker observations
        worker_obs1 : array_like
            batch of next worker observations
        worker_actions : array_like
            batch of worker actions
        update_actor : bool
            specifies whether to update the actor policy of the manager. The
            critic policy is still updated if this value is set to False.

        Returns
        -------
        float
            manager critic loss
        float
            manager actor loss
        """
        # Reshape to match previous behavior and placeholder shape.
        rewards = rewards.reshape(-1, 1)
        terminals1 = terminals1.reshape(-1, 1)

        # Update operations for the critic networks.
        step_ops = [self.manager.critic_loss,
                    self.manager.critic_optimizer[0],
                    self.manager.critic_optimizer[1]]

        feed_dict = {
            self.manager.obs_ph: obs0,
            self.manager.action_ph: actions,
            self.manager.rew_ph: rewards,
            self.manager.obs1_ph: obs1,
            self.manager.terminals1: terminals1
        }

        if update_actor:
            # Actor updates and target soft update operation.
            step_ops += [self.manager.actor_loss,
                         self.cg_optimizer,  # This is what's replaced.
                         self.manager.target_soft_updates]

            feed_dict.update({
                self.worker.obs_ph: worker_obs0,
                self.worker.action_ph: worker_actions,
                self.worker.obs1_ph: worker_obs1,
            })

        # Perform the update operations and collect the critic loss.
        critic_loss, *_vals = self.sess.run(step_ops, feed_dict=feed_dict)

        # Extract the actor loss.
        actor_loss = _vals[2] if update_actor else 0

        return critic_loss, actor_loss