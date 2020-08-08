import gym
from gym import spaces
import rlbench.gym
from rlbench.environment import Environment
from rlbench.action_modes import ArmActionMode, ActionMode
from rlbench.observation_config import ObservationConfig
from rlbench.tasks import ReachTarget as Task
import numpy as np
import os
from multiprocess_vector_env import MultiprocessVectorEnv

from gym.wrappers import ResizeObservation, RescaleAction
from gym.wrappers.flatten_observation import FlattenObservation

class TransposeObs(gym.ObservationWrapper):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # DIRTY!!
        self.observation_space = spaces.Box(low=0.0, high=1.0, shape=(3, 64, 64))

    def observation(self, observation):
        obs = np.transpose(observation, (2, 0, 1))  # hwc --> chw
        return obs


class WristObsWrapper(gym.ObservationWrapper):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.observation_space = self.observation_space['front_rgb']
        # prev_space = self.observation_space['front_rgb']
        # DIRTY!!
        # self.observation_space = spaces.Box(low=0.0, high=1.0, shape=(3, 64, 64))

    def observation(self, observation):
        obs = observation['front_rgb']
        # obs = np.transpose(obs, (2, 0, 1))  # hwc --> chw
        # print('obs.shape (WristObsWrapper)', obs.shape)
        return obs

class GraspActionWrapper(gym.ActionWrapper):
    r"""Rescales the continuous action space of the environment to a range [a,b].
    Example::
        >>> RescaleAction(env, a, b).action_space == Box(a,b)
        True
    """
    def __init__(self, env, action_size):
        assert isinstance(env.action_space, spaces.Box), (
            "expected Box action space, got {}".format(type(env.action_space)))
        super().__init__(env)
        self.action_space = spaces.Box(low=-1.0, high=1.0, shape=(action_size,))

    def action(self, act):
        # Append grasp action (closed)
        return np.concatenate((act, [0.0]), axis=0)

    def reverse_action(self, act):
        return act[:-1]

class NormalizeAction(gym.ActionWrapper):
    def __init__(self, env, speed=0.2):
        super().__init__(env)
        self.speed = speed

    def action(self, act):
        return self.speed * (act / np.linalg.norm(act))



"""A training script of PPO on OpenAI Gym Mujoco environments.

This script follows the settings of https://arxiv.org/abs/1709.06560 as much
as possible.
"""
import argparse
import functools

import gym
import gym.spaces
import numpy as np
import torch
from torch import nn

import pfrl
from pfrl.agents import PPO
from pfrl import experiments
from pfrl import utils


def main():
    import logging

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--gpu", type=int, default=0, help="GPU to use, set to -1 if no GPU."
    )
    parser.add_argument(
        "--env",
        type=str,
        default="reach_target-ee-vision-v1",
        help="OpenAI Gym MuJoCo env to perform algorithm on.",
    )
    parser.add_argument(
        "--num-envs", type=int, default=1, help="Number of envs run in parallel."
    )
    parser.add_argument("--seed", type=int, default=0, help="Random seed [0, 2 ** 32)")
    parser.add_argument(
        "--outdir",
        type=str,
        default="results",
        help=(
            "Directory path to save output files."
            " If it does not exist, it will be created."
        ),
    )
    parser.add_argument(
        "--steps",
        type=int,
        default=2 * 10 ** 6,
        help="Total number of timesteps to train the agent.",
    )
    parser.add_argument(
        "--eval-interval",
        type=int,
        default=100000,
        help="Interval in timesteps between evaluations.",
    )
    parser.add_argument(
        "--eval-n-runs",
        type=int,
        default=100,
        help="Number of episodes run for each evaluation.",
    )
    parser.add_argument(
        "--render", action="store_true", help="Render env states in a GUI window."
    )
    parser.add_argument(
        "--demo", action="store_true", help="Just run evaluation, not training."
    )
    parser.add_argument("--load-pretrained", action="store_true", default=False)
    parser.add_argument(
        "--load", type=str, default="", help="Directory to load agent from."
    )
    parser.add_argument(
        "--log-level", type=int, default=logging.INFO, help="Level of the root logger."
    )
    parser.add_argument(
        "--monitor", action="store_true", help="Wrap env with gym.wrappers.Monitor."
    )
    parser.add_argument(
        "--log-interval",
        type=int,
        default=1000,
        help="Interval in timesteps between outputting log messages during training",
    )
    parser.add_argument(
        "--update-interval",
        type=int,
        default=2048,
        help="Interval in timesteps between model updates.",
    )
    parser.add_argument(
        "--epochs",
        type=int,
        default=10,
        help="Number of epochs to update model for per PPO iteration.",
    )
    parser.add_argument(
        "--action-size",
        type=int,
        default=3,
        help="Action size (needs to match env.action_space)",
    )
    parser.add_argument("--batch-size", type=int, default=64, help="Minibatch size")
    args = parser.parse_args()

    logging.basicConfig(level=args.log_level)

    # Set a random seed used in PFRL
    utils.set_random_seed(args.seed)

    # Set different random seeds for different subprocesses.
    # If seed=0 and processes=4, subprocess seeds are [0, 1, 2, 3].
    # If seed=1 and processes=4, subprocess seeds are [4, 5, 6, 7].
    process_seeds = np.arange(args.num_envs) + args.seed * args.num_envs
    assert process_seeds.max() < 2 ** 32

    args.outdir = experiments.prepare_output_dir(args, args.outdir)

    def make_env(process_idx, test):
        render_mode = 'human' if args.render else None
        env = NormalizeAction(GraspActionWrapper(TransposeObs(ResizeObservation(WristObsWrapper(gym.make(args.env, render_mode=render_mode)), (64, 64))), args.action_size))
        # env = GraspActionWrapper(RescaleAction(FlattenObservation(ResizeObservation(WristObsWrapper(gym.make(args.env)), (64, 64))), -0.5, 0.5))
        # Use different random seeds for train and test envs
        process_seed = int(process_seeds[process_idx])
        env_seed = 2 ** 32 - 1 - process_seed if test else process_seed
        env.seed(env_seed)
        # Cast observations to float32 because our model uses float32
        env = pfrl.wrappers.CastObservationToFloat32(env)
        if args.monitor:
            env = pfrl.wrappers.Monitor(env, args.outdir)
        if args.render:
            env = pfrl.wrappers.Render(env)
        return env

    def make_batch_env(test):
        return MultiprocessVectorEnv(
            [
                functools.partial(make_env, idx, test)
                for idx, env in enumerate(range(args.num_envs))
            ]
        )

    # Only for getting timesteps, and obs-action spaces
    # sample_env = RescaleAction(GraspActionWrapper(FlattenObservation(ResizeObservation(WristObsWrapper(gym.make(args.env)), (64, 64))), args.action_size), -0.5, 0.5)
    # timestep_limit = sample_env.spec.max_episode_steps
    timestep_limit = 200
    # obs_space = sample_env.observation_space
    # obs_space = spaces.Box(low=0, high=1, shape=(64 * 64 * 3,))
    obs_space = spaces.Box(low=0, high=1, shape=(3, 64, 64))
    # action_space = sample_env.action_space
    action_space = spaces.Box(low=-1.0, high=1.0, shape=(args.action_size,))
    print("Observation space:", obs_space)
    print("Action space:", action_space)
    # assert obs_space == spaces.Box(low=0, high=1, shape=(64 * 64 * 3,))
    # assert action_space == spaces.Box(low=-1.0, high=1.0, shape=(args.action_size,))
    # sample_env.close()

    assert isinstance(action_space, gym.spaces.Box)

    # Normalize observations based on their empirical mean and variance
    obs_normalizer = pfrl.nn.EmpiricalNormalization(
        obs_space.shape, clip_threshold=5
    )

    obs_size = obs_space.low.size
    action_size = action_space.low.size
    policy = nn.Sequential(
        nn.Conv2d(in_channels=3, out_channels=32, kernel_size=3, padding=1),
        nn.MaxPool2d(kernel_size=2),
        nn.Conv2d(in_channels=32, out_channels=64, kernel_size=3, padding=1),
        nn.Conv2d(in_channels=64, out_channels=128, kernel_size=3, padding=1),
        nn.Conv2d(in_channels=128, out_channels=128, kernel_size=3, padding=1),
        nn.MaxPool2d(kernel_size=2),
        nn.Conv2d(in_channels=128, out_channels=128, kernel_size=3, padding=1),
        nn.Conv2d(in_channels=128, out_channels=128, kernel_size=3, padding=1),
        nn.MaxPool2d(kernel_size=2),
        nn.Flatten(),
        nn.Linear(8 * 8 * 128, 128),
        nn.ReLU(True),
        nn.Linear(128, 64),
        nn.ReLU(True),
        nn.Linear(64, action_size),
        pfrl.policies.GaussianHeadWithStateIndependentCovariance(
            action_size=action_size,
            var_type="diagonal",
            var_func=lambda x: torch.exp(2 * x),  # Parameterize log std
            var_param_init=0,  # log std = 0 => std = 1
        ),
    )

    vf = nn.Sequential(
        nn.Conv2d(in_channels=3, out_channels=32, kernel_size=3, padding=1),
        nn.MaxPool2d(kernel_size=2),
        nn.Conv2d(in_channels=32, out_channels=64, kernel_size=3, padding=1),
        nn.Conv2d(in_channels=64, out_channels=128, kernel_size=3, padding=1),
        nn.Conv2d(in_channels=128, out_channels=128, kernel_size=3, padding=1),
        nn.MaxPool2d(kernel_size=2),
        nn.Conv2d(in_channels=128, out_channels=128, kernel_size=3, padding=1),
        nn.Conv2d(in_channels=128, out_channels=128, kernel_size=3, padding=1),
        nn.MaxPool2d(kernel_size=2),
        nn.Flatten(),
        nn.Linear(8 * 8 * 128, 128),
        nn.ReLU(True),
        nn.Linear(128, 64),
        nn.ReLU(True),
        nn.Linear(64, 1),
    )

    def _initialize_weights(model):
        for m in model.modules():
            if isinstance(m, nn.Conv2d):
                nn.init.kaiming_normal_(m.weight, mode='fan_out', nonlinearity='relu')
                if m.bias is not None:
                    nn.init.constant_(m.bias, 0)
            elif isinstance(m, nn.BatchNorm2d):
                nn.init.constant_(m.weight, 1)
                nn.init.constant_(m.bias, 0)
            elif isinstance(m, nn.Linear):
                nn.init.normal_(m.weight, 0, 0.01)
                nn.init.constant_(m.bias, 0)

    _initialize_weights(policy)
    _initialize_weights(vf)
    print('weight initialization successful ;)')

    # import torch
    # dummy = torch.tensor(np.zeros((11, 3, 64, 64), dtype=np.float32))
    # import ipdb; ipdb.set_trace()
    # hoge = policy(dummy)

    # Combine a policy and a value function into a single model
    model = pfrl.nn.Branched(policy, vf)

    opt = torch.optim.Adam(model.parameters(), lr=3e-4, eps=1e-5)

    agent = PPO(
        model,
        opt,
        obs_normalizer=obs_normalizer,
        gpu=args.gpu,
        update_interval=args.update_interval,
        minibatch_size=args.batch_size,
        epochs=args.epochs,
        clip_eps_vf=None,
        entropy_coef=0,
        standardize_advantages=True,
        gamma=0.995,
        lambd=0.97,
    )

    if args.load or args.load_pretrained:
        if args.load_pretrained:
            raise Exception("Pretrained models are currently unsupported.")
        # either load or load_pretrained must be false
        assert not args.load or not args.load_pretrained
        if args.load:
            agent.load(args.load)
        else:
            agent.load(utils.download_model("PPO", args.env, model_type="final")[0])

    if args.demo:
        env = make_batch_env(True)
        eval_stats = experiments.eval_performance(
            env=env,
            agent=agent,
            n_steps=None,
            n_episodes=args.eval_n_runs,
            max_episode_len=timestep_limit,
        )
        print(
            "n_runs: {} mean: {} median: {} stdev {}".format(
                args.eval_n_runs,
                eval_stats["mean"],
                eval_stats["median"],
                eval_stats["stdev"],
            )
        )
    else:
        experiments.train_agent_batch_with_evaluation(
            agent=agent,
            env=make_batch_env(False),
            eval_env=make_batch_env(True),
            outdir=args.outdir,
            steps=args.steps,
            eval_n_steps=None,
            eval_n_episodes=args.eval_n_runs,
            eval_interval=args.eval_interval,
            log_interval=args.log_interval,
            max_episode_len=timestep_limit,
            save_best_so_far_agent=True,
        )


if __name__ == "__main__":
    main()
