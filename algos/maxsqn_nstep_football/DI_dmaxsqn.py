import numpy as np
import tensorflow as tf
import time
import ray

from hyperparams_gfootball import HyperParameters, FootballWrapper
from actor_learner import Actor, Learner

import os
import pickle
import multiprocessing
import copy

from collections import deque

import inspect
import json
from ray.rllib.utils.compression import pack, unpack

import gfootball.env as football_env

flags = tf.app.flags
FLAGS = tf.app.flags.FLAGS

# "1_vs_1_easy" '11_vs_11_competition' '11_vs_11_stochastic'
flags.DEFINE_string("env_name", "11_vs_11_stochastic", "game env")
flags.DEFINE_string("exp_name", "Exp1", "experiments name")
flags.DEFINE_integer("num_workers", 6, "number of workers")
flags.DEFINE_string("weights_file", "", "empty means False. "
                                        "[Maxret_weights.pickle] means restore weights from this pickle file.")
flags.DEFINE_float("a_l_ratio", 200, "steps / sample_times")


@ray.remote
class ReplayBuffer:
    """
    A simple FIFO experience replay buffer for SQN_N_STEP agents.
    """

    def __init__(self, Ln, obs_shape, act_shape, size):
        self.obs_shape = obs_shape
        if obs_shape != (115,):
            self.buffer_o = np.array([['0' * 2000] * (Ln + 1)] * size, dtype=np.str)
        else:
            self.buffer_o = np.zeros((size, Ln + 1) + obs_shape, dtype=np.float32)
        self.buffer_a = np.zeros((size, Ln) + act_shape, dtype=np.float32)
        self.buffer_r = np.zeros((size, Ln), dtype=np.float32)
        self.buffer_d = np.zeros((size, Ln), dtype=np.float32)
        self.ptr, self.size, self.max_size = 0, 0, size
        self.steps, self.sample_times = 0, 0

    def store(self, o_queue, a_r_d_queue, worker_index):

        obs, = np.stack(o_queue, axis=1)

        if self.obs_shape != (115,):
            self.buffer_o[self.ptr] = obs
        else:
            self.buffer_o[self.ptr] = np.array(list(obs), dtype=np.float32)

        a, r, d, = np.stack(a_r_d_queue, axis=1)
        self.buffer_a[self.ptr] = np.array(list(a), dtype=np.float32)
        self.buffer_r[self.ptr] = np.array(list(r), dtype=np.float32)
        self.buffer_d[self.ptr] = np.array(list(d), dtype=np.float32)

        self.ptr = (self.ptr + 1) % self.max_size
        self.size = min(self.size + 1, self.max_size)

    def sample_batch(self, batch_size):
        idxs = np.random.randint(0, self.size, size=batch_size)
        self.sample_times += 1

        return dict(obs=self.buffer_o[idxs],
                    acts=self.buffer_a[idxs],
                    rews=self.buffer_r[idxs],
                    done=self.buffer_d[idxs], )

    def add_counts(self, episode_steps):
        self.steps += episode_steps

    def get_counts(self):
        return self.sample_times, self.steps, self.size


@ray.remote
class ParameterServer(object):
    def __init__(self, keys, values, weights_file=""):
        # These values will be mutated, so we must create a copy that is not
        # backed by the object store.

        if weights_file:
            try:
                with open(weights_file, "rb") as pickle_in:
                    self.weights = pickle.load(pickle_in)
                    print("****** weights restored! ******")
            except:
                print("------------------------------------------------")
                print(weights_file)
                print("------ error: weights file doesn't exist! ------")
                exit()
        else:
            values = [value.copy() for value in values]
            self.weights = dict(zip(keys, values))

    def push(self, keys, values):
        values = [value.copy() for value in values]
        for key, value in zip(keys, values):
            self.weights[key] = value

    def pull(self, keys):
        return [self.weights[key] for key in keys]

    def get_weights(self):
        return self.weights

    # save weights to disk
    def save_weights(self, name):
        pickle_out = open(name + "weights.pickle", "wb")
        pickle.dump(self.weights, pickle_out)
        pickle_out.close()


class Cache(object):

    def __init__(self, replay_buffer):
        # cache for training data and model weights
        print('os.pid:', os.getpid())
        self.replay_buffer = replay_buffer
        self.q1 = multiprocessing.Queue(10)
        self.q2 = multiprocessing.Queue(5)
        self.p1 = multiprocessing.Process(target=self.ps_update, args=(self.q1, self.q2))
        self.p1.daemon = True

    def ps_update(self, q1, q2):
        print('os.pid of put_data():', os.getpid())

        q1.put(copy.deepcopy(ray.get(self.replay_buffer.sample_batch.remote(opt.batch_size))))

        while True:
            q1.put(copy.deepcopy(ray.get(self.replay_buffer.sample_batch.remote(opt.batch_size))))

            if not q2.empty():
                keys, values = q2.get()
                ps.push.remote(keys, values)

    def start(self):
        self.p1.start()
        self.p1.join(10)

    def end(self):
        self.p1.terminate()


@ray.remote(num_gpus=1, max_calls=1)
def worker_train(ps, replay_buffer, opt, learner_index):
    agent = Learner(opt, job="learner")
    keys = agent.get_weights()[0]
    weights = ray.get(ps.pull.remote(keys))
    agent.set_weights(keys, weights)

    cache = Cache(replay_buffer)

    cache.start()

    cnt = 1
    while True:
        batch = cache.q1.get()
        if opt.model == "cnn":
            batch['obs'] = np.array([[unpack(o) for o in lno] for lno in batch['obs']])
        agent.train(batch, cnt)
        # TODO cnt % 300 == 0 before
        if cnt % 100 == 0:
            cache.q2.put(agent.get_weights())
        cnt += 1


@ray.remote
def worker_rollout(ps, replay_buffer, opt, worker_index):
    worker_epsilon = 0
    if opt.epsilon != 0:
        worker_epsilon = opt.epsilon ** (1 + worker_index / (opt.num_workers - 1) * opt.epsilon_alpha)
        print("worker_index:", worker_index, "worker_epsilon:", worker_epsilon)
    local_epsilon = opt.epsilon

    max_steps = 0
    mu, sigma = 0, 0.2
    while True:
        # ------ env set up ------
        # env = gym.make(opt.env_name)

        while True:
            s = np.random.normal(mu, sigma, 1)
            if 0 < s[0] < 1:
                using_difficulty = int(s[0] // 0.05 + 1)
                break
        print(worker_index, "using difficulty:", using_difficulty)
        if opt.game_difficulty != 0:
            env = football_env.create_environment(env_name=opt.rollout_env_name + '_' + str(using_difficulty),
                                                  stacked=opt.stacked, representation=opt.representation, render=False)
        else:
            env = football_env.create_environment(env_name=opt.rollout_env_name,
                                                  stacked=opt.stacked, representation=opt.representation, render=False)
        env = FootballWrapper(env, opt.action_repeat, opt.reward_scale)
        # ------ env set up end ------

        agent = Actor(opt, job="worker")
        keys = agent.get_weights()[0]

        ################################## deques

        o_queue = deque([], maxlen=opt.Ln + 1)
        a_r_d_queue = deque([], maxlen=opt.Ln)

        ################################## deques

        o, r, d, ep_ret, ep_len = env.reset(), 0, False, 0, 0

        ################################## deques reset
        t_queue = 1
        if opt.model == "cnn":
            compressed_o = pack(o)
            o_queue.append((compressed_o,))
        else:
            o_queue.append((o,))

        ################################## deques reset

        weights = ray.get(ps.pull.remote(keys))
        agent.set_weights(keys, weights)

        # for t in range(total_steps):
        t = 0

        while True:
            if opt.epsilon != 0:
                if local_epsilon != opt.epsilon:
                    worker_epsilon = opt.epsilon ** (1 + worker_index / (opt.num_workers - 1) * opt.epsilon_alpha)
                    local_epsilon = opt.epsilon

            # don't need to random sample action if load weights from local.
            if t > opt.start_steps or opt.weights_file:
                if np.random.rand() > worker_epsilon:
                    a = agent.get_action(o, deterministic=False)
                else:
                    a = env.action_space.sample()
            else:
                a = env.action_space.sample()
                t += 1
            # Step the env
            o2, r, d, _ = env.step(a)

            ep_ret += r
            ep_len += 1

            # Ignore the "done" signal if it comes from hitting the time
            # horizon (that is, when it's an artificial terminal signal
            # that isn't based on the agent's state)
            # d = False if ep_len*opt.action_repeat >= opt.max_ep_len else d

            o = o2

            #################################### deques store

            a_r_d_queue.append((a, r, d,))
            if opt.model == "cnn":
                compressed_o2 = pack(o2)
                o_queue.append((compressed_o2,))
            else:
                o_queue.append((o2,))

            # scheme 1:
            # TODO  and t_queue % 2 == 0: %1 lead to q smaller
            if t_queue >= opt.Ln and t_queue % opt.save_freq == 0:
                replay_buffer.store.remote(o_queue, a_r_d_queue, worker_index)

            # scheme 2:
            # if t_queue % opt.Ln == 0:
            #     replay_buffer.store.remote(o_queue, a_r_d_queue, worker_index)
            #
            # if d and t_queue % opt.Ln != 0:
            #     for _0 in range(opt.Ln - t_queue % opt.Ln):
            #         a_r_d_queue.append((np.zeros(opt.a_shape, dtype=np.float32), 0.0, True,))
            #         if opt.model == "cnn":
            #             o_queue.append((pack(np.zeros(opt.obs_dim, dtype=np.float32)),))
            #         else:
            #             o_queue.append((np.zeros(opt.obs_dim, dtype=np.float32),))
            #     replay_buffer.store.remote(o_queue, a_r_d_queue, worker_index)
            ###

            t_queue += 1

            #################################### deques store

            # End of episode. Training (ep_len times).
            if d or (ep_len * opt.action_repeat >= opt.max_ep_len):
                replay_buffer.add_counts.remote(ep_len * opt.action_repeat)
                sample_times, steps, _ = ray.get(replay_buffer.get_counts.remote())

                while sample_times > 0 and (steps - opt.start_steps) / sample_times > opt.a_l_ratio:
                    sample_times, steps, _ = ray.get(replay_buffer.get_counts.remote())
                    time.sleep(0.1)

                print('rollout_ep_len:', ep_len * opt.action_repeat, 'rollout_ep_ret:', ep_ret)

                if steps > opt.start_steps:
                    # update parameters every episode
                    weights = ray.get(ps.pull.remote(keys))
                    agent.set_weights(keys, weights)

                o, r, d, ep_ret, ep_len = env.reset(), 0, False, 0, 0

                ################################## deques reset
                t_queue = 1
                if opt.model == "cnn":
                    compressed_o = pack(o)
                    o_queue.append((compressed_o,))
                else:
                    o_queue.append((o,))

                ################################## deques reset

                if sample_times // int(1e6) > max_steps:
                    mu += 0.05
                    max_steps += 1
                    break


@ray.remote
def worker_test(ps, replay_buffer, opt):
    agent = Actor(opt, job="main")

    keys, weights = agent.get_weights()

    time0 = time1 = time.time()
    sample_times1, steps, size = ray.get(replay_buffer.get_counts.remote())

    max_steps = 0
    epsilon_score = 1
    while True:

        if opt.game_difficulty != 0:
            # ------ env set up ------
            test_env = football_env.create_environment(env_name=opt.env_name + '_' + str(opt.game_difficulty),
                                                       stacked=opt.stacked, representation=opt.representation,
                                                       render=False)
            # game_difficulty == 1 mean 0.05, 2 mean 0.1, 3 mean 0.15 ...
            # opt.game_difficulty += 1
        else:
            test_env = football_env.create_environment(env_name=opt.env_name,
                                                       stacked=opt.stacked, representation=opt.representation,
                                                       render=False)

        current_ret = 0

        # test_env = FootballWrapper(test_env)

        # test_env = gym.make(opt.env_name)
        # ------ env set up end ------

        while current_ret < opt.threshold_score:
            # weights_all for save it to local
            weights_all = ray.get(ps.get_weights.remote())
            weights = [weights_all[key] for key in keys]

            agent.set_weights(keys, weights)

            sample_times2, steps, size = ray.get(replay_buffer.get_counts.remote())
            time2 = time.time()

            ep_ret = agent.test(test_env, replay_buffer)
            current_ret = ep_ret
            if opt.epsilon != 0 and current_ret > epsilon_score:
                opt.epsilon -= 0.035
                epsilon_score += 1

            print("----------------------------------")
            print("| test_reward:", ep_ret)
            print("| sample_times:", sample_times2)
            # TODO
            print("| steps:", steps)
            print("| buffer_size:", size)
            print("| actual a_l_ratio:", str((steps - opt.start_steps) / (sample_times2 + 1))[:4])
            print('- update frequency:', (sample_times2 - sample_times1) / (time2 - time1), 'total time:',
                  time2 - time0)
            print("----------------------------------")

            if steps // int(1e6) > max_steps:
                pickle_out = open(opt.save_dir + "/" + str(steps // int(1e6))[:3] + "M_weights.pickle", "wb")
                pickle.dump(weights_all, pickle_out)
                pickle_out.close()
                print("****** Weights saved by time! ******")
                max_steps = steps // int(1e6)

            if ep_ret > opt.max_ret:
                pickle_out = open(opt.save_dir + "/" + "Max_weights.pickle", "wb")
                pickle.dump(weights_all, pickle_out)
                pickle_out.close()
                print("****** Weights saved by maxret! ******")
                opt.max_ret = ep_ret

            time1 = time2
            sample_times1 = sample_times2

            time.sleep(5)


if __name__ == '__main__':

    # ray.init(object_store_memory=1000000000, redis_max_memory=1000000000)
    ray.init()

    # ------ HyperParameters ------
    opt = HyperParameters(FLAGS.env_name, FLAGS.exp_name, FLAGS.num_workers, FLAGS.a_l_ratio,
                          FLAGS.weights_file)
    All_Parameters = copy.deepcopy(vars(opt))
    All_Parameters["wrapper"] = inspect.getsource(FootballWrapper)
    import importlib

    scenario = importlib.import_module('gfootball.scenarios.{}'.format(opt.rollout_env_name))
    All_Parameters["rollout_env_class"] = inspect.getsource(scenario.build_scenario)
    All_Parameters["obs_space"] = ""
    All_Parameters["act_space"] = ""

    try:
        os.makedirs(opt.save_dir)
    except OSError:
        pass
    with open(opt.save_dir + "/" + 'All_Parameters.json', 'w') as fp:
        json.dump(All_Parameters, fp, indent=4, sort_keys=True)

    # ------ end ------

    if FLAGS.weights_file:
        ps = ParameterServer.remote([], [], weights_file=FLAGS.weights_file)
    else:
        net = Learner(opt, job="main")
        all_keys, all_values = net.get_weights()
        ps = ParameterServer.remote(all_keys, all_values)

    # Experience buffer
    replay_buffer = ReplayBuffer.remote(Ln=opt.Ln, obs_shape=opt.o_shape, act_shape=opt.a_shape, size=opt.replay_size)

    # Start some training tasks.
    task_rollout = [worker_rollout.remote(ps, replay_buffer, opt, i) for i in range(FLAGS.num_workers)]

    if opt.weights_file:
        fill_steps = opt.start_steps / 100
    else:
        fill_steps = opt.start_steps
    # store at least start_steps in buffer before training
    _, steps, _ = ray.get(replay_buffer.get_counts.remote())
    while steps < fill_steps:
        _, steps, _ = ray.get(replay_buffer.get_counts.remote())
        print('fill steps before learn:', steps)
        time.sleep(1)

    task_train = [worker_train.remote(ps, replay_buffer, opt, i) for i in range(opt.num_learners)]

    while True:
        task_test = worker_test.remote(ps, replay_buffer, opt)
        ray.wait([task_test, ])
