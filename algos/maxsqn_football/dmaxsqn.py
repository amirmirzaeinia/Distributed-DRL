import numpy as np
import tensorflow as tf
import time
import ray
import gym
from gym.spaces import Box, Discrete
from collections import deque

from hyperparams_gfootball import HyperParameters, FootballWrapper
from actor_learner import Actor, Learner

import os
import pickle
import multiprocessing
import copy
import signal

import inspect
import json

import gfootball
import gfootball.env as football_env

# flags = tf.app.flags
# FLAGS = tf.app.flags.FLAGS
#
#
# flags.DEFINE_string("exp_name", "Exp1", "experiments name")
# flags.DEFINE_integer("total_epochs", 500, "total_epochs")
# flags.DEFINE_integer("num_workers", 1, "number of workers")
# flags.DEFINE_integer("num_learners", 1, "number of learners")
# flags.DEFINE_string("is_restore", "False", "True or False. True means restore weights from pickle file.")
# flags.DEFINE_float("a_l_ratio", 2, "steps / sample_times")


@ray.remote
class ReplayBuffer_N:
    """
    A simple FIFO experience replay buffer for SQN_N_STEP agents.
    """

    def __init__(self, Ln, obs_shape, act_shape, size):
        self.buffer_o = np.zeros((size, Ln + 1)+obs_shape, dtype=np.float32)
        self.buffer_a = np.zeros((size, Ln)+act_shape, dtype=np.float32)
        self.buffer_r = np.zeros((size, Ln), dtype=np.float32)
        self.buffer_d = np.zeros((size, Ln), dtype=np.float32)
        self.ptr, self.size, self.max_size = 0, 0, size
        self.steps, self.sample_times = 0, 0
        self.worker_pool = set()
        print("ray.get_gpu_ids(): {}".format(ray.get_gpu_ids()))
        print("CUDA_VISIBLE_DEVICES: {}".format(os.environ["CUDA_VISIBLE_DEVICES"]))

    def store(self, o_queue, a_r_d_queue, worker_index):
        obs, = np.stack(o_queue, axis=1)
        self.buffer_o[self.ptr] = np.array(list(obs), dtype=np.float32)

        a, r, d, = np.stack(a_r_d_queue, axis=1)
        self.buffer_a[self.ptr] = np.array(list(a), dtype=np.float32)
        self.buffer_r[self.ptr] = np.array(list(r), dtype=np.float32)
        self.buffer_d[self.ptr] = np.array(list(d), dtype=np.float32)

        self.ptr = (self.ptr + 1) % self.max_size
        self.size = min(self.size + 1, self.max_size)

        self.steps += 1
        self.worker_pool.add(worker_index)

    def sample_batch(self, batch_size=128):
        idxs = np.random.randint(0, self.size, size=batch_size)
        self.sample_times += 1
        return dict(obs=self.buffer_o[idxs],
                    acts=self.buffer_a[idxs],
                    rews=self.buffer_r[idxs],
                    done=self.buffer_d[idxs],)


    def get_counts(self):
        return self.sample_times, self.steps, self.size, len(self.worker_pool)

    def empty_worker_pool(self):
        self.worker_pool = set()


@ray.remote
class ParameterServer(object):
    def __init__(self, keys, values, is_restore=False):
        # These values will be mutated, so we must create a copy that is not
        # backed by the object store.

        if is_restore:
            try:
                pickle_in = open("weights.pickle", "rb")
                self.weights = pickle.load(pickle_in)
                print("****** weights restored! ******")
            except:
                print("------ error: weights.pickle doesn't exist! ------")
        else:
            values = [value.copy() for value in values]
            self.weights = dict(zip(keys, values))
        print("ray.get_gpu_ids(): {}".format(ray.get_gpu_ids()))
        print("CUDA_VISIBLE_DEVICES: {}".format(os.environ["CUDA_VISIBLE_DEVICES"]))
    # def push(self, keys, values):
    #     for key, value in zip(keys, values):
    #         self.weights[key] += value

    # TODO push gradients or parameters
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
        pickle_out = open(name+"weights.pickle", "wb")
        pickle.dump(self.weights, pickle_out)
        pickle_out.close()


class Cache(object):

    def __init__(self, replay_buffer):
        # cache for training data and model weights
        print('os.pid:', os.getpid())
        self.replay_buffer = replay_buffer
        self.q1 = multiprocessing.Queue(10)
        self.q2 = multiprocessing.Queue(5)
        self.p1 = multiprocessing.Process(target=self.ps_update, args=(self.q1, self.q2, self.replay_buffer))

    def ps_update(self, q1, q2, replay_buffer):
        print('os.pid of put_data():', os.getpid())

        q1.put(copy.deepcopy(ray.get(replay_buffer.sample_batch.remote(opt.batch_size))))

        while True:
            q1.put(copy.deepcopy(ray.get(replay_buffer.sample_batch.remote(opt.batch_size))))

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
    print("ray.get_gpu_ids(): {}".format(ray.get_gpu_ids()))
    print("CUDA_VISIBLE_DEVICES: {}".format(os.environ["CUDA_VISIBLE_DEVICES"]))

    agent = Learner(opt, job="learner")
    keys = agent.get_weights()[0]
    weights = ray.get(ps.pull.remote(keys))
    agent.set_weights(keys, weights)

    cache = Cache(replay_buffer)

    cache.start()

    def signal_handler(signal, frame):
        cache.end()
        exit()

    signal.signal(signal.SIGINT, signal_handler)

    cnt = 1
    while True:
        batch = cache.q1.get()
        agent.train(batch, cnt)
        if cnt % 300 == 0:
            cache.q2.put(agent.get_weights())
        cnt += 1


@ray.remote
def worker_rollout(ps, replay_buffer, opt, worker_index):
    print("ray.get_gpu_ids(): {}".format(ray.get_gpu_ids()))
    print("CUDA_VISIBLE_DEVICES: {}".format(os.environ["CUDA_VISIBLE_DEVICES"]))

    # ------ env set up ------
    # env = gym.make(opt.env_name)
    env = football_env.create_environment(env_name=opt.rollout_env_name,
                                          representation='simple115', render=False)
    env = FootballWrapper(env)
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
    o_queue.append((o,))

    ################################## deques reset


    # epochs = opt.total_epochs // opt.num_workers
    total_steps = opt.steps_per_epoch * opt.total_epochs

    weights = ray.get(ps.pull.remote(keys))
    agent.set_weights(keys, weights)

    # for t in range(total_steps):
    t = 0
    while True:
        if t > opt.start_steps_per_worker or opt.is_restore:
            a = agent.get_action(o, deterministic=False)
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
        d = False if ep_len == opt.max_ep_len else d

        # Super critical, easy to overlook step: make sure to update
        # most recent observation!
        o = o2

        #################################### deques store

        a_r_d_queue.append( (a, r, d,) )
        o_queue.append((o2,))

        if t_queue % opt.Ln == 0:
            replay_buffer.store.remote(o_queue, a_r_d_queue, worker_index)

        if d and t_queue % opt.Ln != 0:
            for _0 in range(opt.Ln - t_queue % opt.Ln):
                a_r_d_queue.append((np.zeros(opt.a_shape, dtype=np.float32), 0.0, True,))
                o_queue.append((np.zeros((opt.obs_dim,), dtype=np.float32), ))
            replay_buffer.store.remote(o_queue, a_r_d_queue, worker_index)

        t_queue += 1

        #################################### deques store


        # End of episode. Training (ep_len times).
        if d or (ep_len == opt.max_ep_len):

            sample_times, steps, _, _ = ray.get(replay_buffer.get_counts.remote())

            while sample_times > 0 and (steps-opt.start_steps) / sample_times > opt.a_l_ratio:
                sample_times, steps, _, _ = ray.get(replay_buffer.get_counts.remote())
                time.sleep(0.1)

            print('rollout_ep_len:', ep_len, 'rollout_ep_ret:', ep_ret)
            # update parameters every episode
            weights = ray.get(ps.pull.remote(keys))
            agent.set_weights(keys, weights)

            o, r, d, ep_ret, ep_len = env.reset(), 0, False, 0, 0

            ################################## deques reset
            t_queue = 1
            o_queue.append((o,))

            ################################## deques reset


@ray.remote
def worker_test(ps, replay_buffer, opt):

    agent = Actor(opt, job="main")

    keys, weights = agent.get_weights()

    time0 = time1 = time.time()
    sample_times1, steps, size, _ = ray.get(replay_buffer.get_counts.remote())
    max_ret = -1000
    max_sample_times = 0

    # ------ env set up ------
    test_env = football_env.create_environment(env_name=opt.rollout_env_name,
                                          representation='simple115', render=False)
    # test_env = FootballWrapper(test_env)

    # test_env = gym.make(opt.env_name)
    # ------ env set up end ------

    while True:
        # weights_all for save it to local
        weights_all = ray.get(ps.get_weights.remote())
        weights = [weights_all[key] for key in keys]

        agent.set_weights(keys, weights)

        # In case the env crushed

        ep_ret = agent.test(test_env, replay_buffer)

        # ep_ret = agent.test(test_env, replay_buffer)

        sample_times2, steps, size, worker_alive = ray.get(replay_buffer.get_counts.remote())
        time2 = time.time()
        # print("test_reward:", ep_ret, "sample_times:", sample_times2, "steps:", steps, "buffer_size:", size,
        #       "actual a_l_ratio:", str(steps/(sample_times2+1))[:4], "num of alive worker:", worker_alive)
        print("----------------------------------")
        print("| test_reward:", ep_ret)
        print("| sample_times:", sample_times2)
        print("| steps:", steps)
        print("| env_steps:", steps*opt.Ln)
        print("| buffer_size:", size)
        print("| actual a_l_ratio:", str((steps-opt.start_steps)/(sample_times2+1))[:4])
        print("| num of alive worker:", worker_alive)
        print('- update frequency:', (sample_times2-sample_times1)/(time2-time1), 'total time:', time2 - time0)
        print("----------------------------------")

        if sample_times2 // int(1e6) > max_sample_times:
            pickle_out = open(opt.save_dir + "/" + str(sample_times2 // int(1e6))[:5]+"M_weights.pickle", "wb")
            pickle.dump(weights_all, pickle_out)
            pickle_out.close()
            print("****** Weights saved by time! ******")
            max_sample_times = sample_times2 // int(1e6)

        if ep_ret > max_ret:
            pickle_out = open(opt.save_dir + "/" + "Max_weights.pickle", "wb")
            pickle.dump(weights_all, pickle_out)
            pickle_out.close()
            print("****** Weights saved by maxret! ******")
            max_ret = ep_ret

        time1 = time2
        sample_times1 = sample_times2

        replay_buffer.empty_worker_pool.remote()
        time.sleep(5)


if __name__ == '__main__':

    ray.init(object_store_memory=1000000000, redis_max_memory=1000000000)
    # ray.init()
    print("ray.get_gpu_ids(): {}".format(ray.get_gpu_ids()))

    # ------ HyperParameters ------
    opt = HyperParameters()
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

    # Create a parameter server with some random weights.
    if opt.is_restore == "True":
        ps = ParameterServer.remote([], [], is_restore=True)
    else:
        net = Learner(opt, job="main")
        all_keys, all_values = net.get_weights()
        ps = ParameterServer.remote(all_keys, all_values)

    # Experience buffer
    if isinstance(opt.act_space, Box):
        a_dim = opt.act_dim
    elif isinstance(opt.act_space, Discrete):
        a_dim = 1

    replay_buffer = ReplayBuffer_N.remote(Ln=opt.Ln, obs_shape=opt.o_shape, act_shape=opt.a_shape,
                                                size=opt.replay_size)
    # Start some training tasks.
    task_rollout = [worker_rollout.remote(ps, replay_buffer, opt, i) for i in range(opt.num_workers)]

    # store at least start_steps in buffer before training
    _, steps, _, _ = ray.get(replay_buffer.get_counts.remote())
    while steps < opt.start_steps:
        _, steps, _, _ = ray.get(replay_buffer.get_counts.remote())
        print('start_steps:', steps)
        time.sleep(1)

    task_train = [worker_train.remote(ps, replay_buffer, opt, i) for i in range(opt.num_learners)]

    while True:
        task_test = worker_test.remote(ps, replay_buffer, opt)
        ray.wait([task_test, ])