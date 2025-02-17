import gym_mupen64plus
# %%
import gym, torch, cv2, time
import numpy as np
import torch.nn as nn
from torch.distributions import Categorical
import torch.nn.functional as F

import matplotlib.pyplot as plt
import matplotlib.animation as animation

from collections import deque
import pickle
from os import path
import itertools

# %%
device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
version = input("Version: ")

# %%
# Game environment wrapper
_ACTION_DIM = 15

def process_obs(image):
    image = cv2.resize(image, (84, 84))
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)

    return gray

def map_action_space(action_i):
    action = [0] * 8
    if action_i == 0:
        pass
    elif action_i == 1:
        action[0] = 127
    elif action_i == 2:
        action[0] = -128
    elif action_i == 3:
        action[1] = 127
    elif action_i == 4:
        action[1] = -128
    elif action_i == 5:
        action[0] = 127
        action[1] = 127
    elif action_i == 6:
        action[0] = 127
        action[1] = -128
    elif action_i == 7:
        action[0] = -128
        action[1] = 127
    elif action_i == 8:
        action[0] = -128
        action[1] = -128
    else:
        action[action_i - 7] = 1
    return action

class FrameStack:
    def __init__(self, stack_size):
        self.frames = deque(maxlen=stack_size)

    def reset(self, frame):
        self.frames.extend([frame] * 4)
        stack = np.stack(self.frames, axis=0)
        return stack

    def __call__(self, frame):
        if len(self.frames) == 0:
            self.frames.extend([frame] * 4)
        else:
            self.frames.append(frame)
        stack = np.stack(self.frames, axis=0)
        return stack

class SmashEnv:
    def __init__(self, args):
        self.env = gym.make(args.env_id)
        self.args = args
    
    def step(self, action):
        if "Smash" in args.env_id:
            action = map_action_space(action)
            
        obs, reward, done, info = self.env.step(action)
        state = self.args.framestack(process_obs(obs))
        return state, reward, done, info
    
    def reset(self):
        obs = self.env.reset()
        state = self.args.framestack.reset(process_obs(obs))
        return state
    
    def close(self): self.env.close()
# %%
# Recording functions
def save_video(args):
  deep_frames = []
  n = len(args.memory.buffer)
  for experience in itertools.islice(args.memory.buffer,n-args.max_steps,n):
    f = experience[0]
    deep_frames += [f[-1]]
  plt.figure(figsize=(deep_frames[0].shape[1] / 72.0, deep_frames[0].shape[0] / 72.0), dpi = 72)                                          
  patch = plt.imshow(deep_frames[0])
  plt.axis('off')
  animate = lambda i: patch.set_data(deep_frames[i])
  ani = animation.FuncAnimation(plt.gcf(), animate, frames=len
  (deep_frames), interval = 50)

  Writer = animation.writers['ffmpeg']
  writer = Writer(fps=15, metadata=dict(artist='Me'), bitrate=1800)
  ani.save('recorded_data/training%s_%i.mp4' % (version, args.episode), writer=writer)

def log(args, update=False, episode = False):
    if update:
        args.losses.append(args.loss)
        if args.iteration % args.print_period == 0:
            print(args.loss)
    if episode:
        if args.episode % args.save_period == 0:
            save_video(args)
        print("%i Accumulated Reward: %f" % (args.episode, sum(args.rewards[-args.episode_length:])))

# %%
# Experience replay buffer
class Memory():
    def __init__(self, max_size):
        self.buffer = deque(maxlen = max_size)
    
    def add(self, experience):
        self.buffer.append(experience)
    
    def sample(self, batch_size):
        buffer_size = len(self.buffer)
        index = np.random.choice(np.arange(buffer_size),
                                size = batch_size,
                                replace = True)
        
        return [self.buffer[i] for i in index]

# fill memory with random transitions
def fill_memory(args):
    env = args.env
    for episode_idx in range(20):
        state = env.reset()
        for step_idx in range(args.max_steps):
            last_state = state
            action = np.random.rand(args.action_dim)
            state, reward, done, _ = env.step(action)
            state = state
            args.memory.add((last_state, action, reward, state, done))
# %%
# Training functions

# linearly decays epsilon
def epsilon_scheduler(args):
  epsilon = max(0, args.epsilon - args.iteration * args.epsilon_decay)
  return epsilon

def get_loss(args):
    batch = args.memory.sample(args.batch_size)
    states, actions, rewards, next_states, dones = list(zip(*batch))
    states_t = torch.Tensor(states).to(device)
    next_states_t = torch.Tensor(next_states).to(device)
    rewards_t = torch.Tensor(rewards).to(device)
    dones_t = torch.Tensor(dones).bool().to(device)

    Qs = args.model(states_t)
    next_Qs = args.target_model(next_states_t).detach()

    preds_t = Qs[np.arange(args.batch_size), actions]

    targets_t = args.gamma * rewards_t + next_Qs.max(1)[0] * ~dones_t
    
    loss = args.loss_func(preds_t, targets_t)
    
    args.loss = loss.item(); args.targets = targets_t; args.preds = preds_t
    
    return loss

def update(args):
    args.model.train()
    args.optimizer.zero_grad()

    loss = get_loss(args)
    loss.backward()

    if args.iteration % args.prop_steps == 0:
        args.target_model.load_state_dict(args.model.state_dict())

    log(args, update=True)
    args.iteration += 1

# %%
# Get action
def get_action(args, state):
    epsilon = epsilon_scheduler(args)
    args.model.eval()

    if np.random.rand() < epsilon:
        action = np.random.randint(args.action_dim)
    else:
        state_t = torch.Tensor(state).unsqueeze(0).to(device)
        Q = args.model(state_t)
        action = torch.max(Q, 1)[1].item()
    return action

# %%
# Initialize environment
def get_model(input_dim, output_dim):
    return nn.Sequential(
        # 84, 84
        nn.Conv2d(input_dim, 32, 7, 2, 3), # 42, 42
        nn.ReLU(),
        nn.Conv2d(32, 64, 3, 2, 1), # 21, 21
        nn.ReLU(),
        nn.Conv2d(64, 128, 3, 2, 1), # 11, 11
        nn.ReLU(),
        nn.Flatten(), # 128 * 11 * 11
        nn.Linear(128 * 11 * 11, 1000),
        nn.ReLU(),
        nn.Linear(1000, output_dim)
    )

def save(args):
    torch.save(args.model.state_dict(), './recorded_data/DQN%s.pth' % version)
    #args.model = None
    #with open("./recorded_data/args.pyobj", "w") as f:
    #    pickle.dump(args, f)

def load(args):
    if path.exists("./recorded_data/DQN%s.pth" % version):
      args.model.load_state_dict(torch.load("./recorded_data/DQN%s.pth" % version))
    #with open("buffer.pyobj", "r") as f:
    #    buffer = pickle.load(f)

def init_env(args):
    args.env = SmashEnv(args)
    args.memory = Memory(args.memory_size)
    args.model = get_model(args.obs_dim, args.action_dim).to(device)
    args.target_model = get_model(args.obs_dim, args.action_dim).to(device)
    args.target_model.load_state_dict(args.model.state_dict())
    args.optimizer = torch.optim.Adam(args.model.parameters(), args.lr)

    load(args)
    fill_memory(args)

# %%
# Default args
class Args: 
    def __init__(self):
        self.env_id = "Smash-dk-v0"

        self.nb_stacks = 4
        self.obs_dim = self.nb_stacks
        self.action_dim = _ACTION_DIM

        self.loss_func = nn.MSELoss()
        self.framestack = FrameStack(self.nb_stacks)

        self.epsilon = 0.2
        self.epsilon_decay = 0.000002
        self.gamma = 0.99
        self.lr = 0.003
        self.batch_size = 128

        self.memory_size = 100000
        self.nb_episodes = 100
        self.max_steps = 2000
        self.prop_steps = 50
        self.update_steps = 30
        self.save_period = 10
        self.print_period = 10


        self.iteration = 0
        self.episode = 0
        self.episode_length = 0
        self.losses = []
        self.actions = []
        self.rewards = []
        self.targets = None
        self.preds = None
# %%
# Start
args = Args()
init_env(args)

# %%
# Training loop
def train ():
    env = args.env
    for episode_idx in range(args.nb_episodes):
        state = env.reset()

        for step_idx in range(args.max_steps):
            last_state = state
            action = get_action(args, last_state)

            state, reward, done, _ = env.step(action)
            state = state

            args.memory.add((last_state, action, reward, state, done))
            
            update(args)

            args.rewards.append(reward); args.actions.append(action)

        args.episode_length = step_idx; args.episode += 1
        log(args, episode = True)

        if episode_idx % args.save_period == 0:
            save(args)

    save(args)