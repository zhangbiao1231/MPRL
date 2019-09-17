# ========================================================================
#
# Imports
#
# ========================================================================
import argparse
import os
import shutil
import numpy as np
import pandas as pd
import time
from datetime import timedelta
import warnings
import pickle
from stable_baselines.ddpg.policies import MlpPolicy as ddpgMlpPolicy
from stable_baselines.deepq.policies import MlpPolicy as dqnMlpPolicy
from stable_baselines.deepq.policies import LnMlpPolicy as dqnLnMlpPolicy
from stable_baselines.deepq.policies import CnnPolicy as dqnCnnPolicy
from stable_baselines.deepq.policies import LnCnnPolicy as dqnLnCnnPolicy
from stable_baselines.common.policies import MlpPolicy
from stable_baselines.common.policies import MlpLnLstmPolicy
from stable_baselines.deepq.policies import FeedForwardPolicy
from stable_baselines.common.policies import LstmPolicy
from stable_baselines.common.vec_env import DummyVecEnv, SubprocVecEnv
from stable_baselines.ddpg.noise import (
    NormalActionNoise,
    OrnsteinUhlenbeckActionNoise,
    AdaptiveParamNoiseSpec,
)
from stable_baselines import DDPG, A2C, DQN, PPO2
import engine
import engine0D
import agents
import utilities
from gym import spaces


# Custom MLP policy of two layers of size 32 each
class CustomDQNPolicy(FeedForwardPolicy):
# class CustomDQNPolicy(LstmPolicy):
    def __init__(self, *args, **kwargs):
        super(CustomDQNPolicy, self).__init__(*args, **kwargs,
                                           layers=[32, 32],
                                           layer_norm=False,
                                           feature_extraction="mlp")

# ========================================================================
#
# Functions
#
# ========================================================================
def callback(_locals, _globals):
    """
    Callback for agent
    :param _locals: (dict)
    :param _globals: (dict)
    """
    global best_reward

    # After each episode, log the reward
    done = False
    if isinstance(_locals["self"], DDPG):
        if _locals["done"]:
            done = True
            info = [
                _locals["episodes"],
                _locals["episode_step"],
                _locals["total_steps"],
                _locals["episode_reward"],
            ]

    elif isinstance(_locals["self"], A2C):
        warnings.warn("Callback not implemented for this agent")

    elif isinstance(_locals["self"], DQN):
        try:
            done = _locals["done"]
        except KeyError:
            pass

        if done:
            info = [
                _locals["num_episodes"] - 1,
                _locals["info"]["internals"].name,
                _locals["_"],
                _locals["episode_rewards"][-2],
            ]

    else:
        warnings.warn("Callback not implemented for this agent")

    if done:
        df = pd.read_csv(logname)
        df.loc[len(df)] = info
        df.to_csv(logname, index=False)

        # save the agent if it is any good
        if df.episode_reward.iloc[-1] > best_reward:
            print("Saving new best agent")
            best_reward = df.episode_reward.iloc[-1]
            _locals["self"].save(os.path.join(logdir, "best_agent.pkl"))


# ========================================================================
#
# Main
#
# ========================================================================
if __name__ == "__main__":

    # Parse arguments
    parser = argparse.ArgumentParser(description="Train and evaluate an agent")
    parser.add_argument(
        "-a",
        "--agent",
        help="Agent to train and evaluate",
        type=str,
        default="calibrated",
        choices=["calibrated", "ddpg", "a2c", "dqn","ppo"],
    )
    parser.add_argument(
        "-t",
        "--total_timesteps",
        help="Total number of steps for training",
        type=int,
        default=100,
    )
    parser.add_argument(
        "-s", "--nsteps", help="Total steps in a given episode", type=int, default=201
    )
    parser.add_argument(
        "-nep", help="Total number of episodes to train over", type=int, default=100
    )
    parser.add_argument("--nranks", help="Number of MPI ranks", type=int, default=1)
    parser.add_argument(
        "--use_pretrained",
        help="Use a pretrained network as a starting point",
        action="store_true",
    )
    parser.add_argument(
        "--use_qdot", help="Use a Qdot as an action", action="store_true"
    )
    parser.add_argument(
        "--use_discrete", help="Use a discrete action space", action="store_true"
    )
    parser.add_argument(
        "--use_random_start", help="Start with a random crank angle", action="store_true"
    )
    parser.add_argument(
        "--subtract_baseline", help="Subtract baseline reward", action="store_true"
    )
    parser.add_argument(
        "--use_engine_0D", help="Use the Cantera 0D reactor", action="store_true"
    )
    parser.add_argument(
        "--use_best_agent", help="Use the best agent, do not train", action="store_true"
    )
    args = parser.parse_args()

    # Setup
    start = time.time()
    np.random.seed(45473)
    logdir = f"{args.agent}"
    if (not args.use_best_agent):
        if os.path.exists(logdir):
            shutil.rmtree(logdir)
        os.makedirs(logdir)
    logname = os.path.join(logdir, "logger.csv")
    logs = pd.DataFrame(
        columns=["episode", "episode_step", "total_steps", "episode_reward"]
    )
    logs.to_csv(logname, index=False)
    pickle.dump(args, open(os.path.join(logdir, "args.pkl"), "wb"))
    best_reward = -np.inf

    # Initialize the engine
    if args.use_engine_0D:
        T0, p0 = engine0D.calibrated_engine_ic()
        eng = engine0D.Engine(
            T0=T0,
            p0=p0,
        )
    else:
        T0, p0 = engine.calibrated_engine_ic()
        eng = engine.Engine(
            T0=T0,
            p0=p0,
            nsteps=args.nsteps,
            use_qdot=args.use_qdot,
            discrete_action=args.use_discrete,
            use_random_start=args.use_random_start,
            subtract_baseline=args.subtract_baseline,
        )

    # Create the agent and train
    if args.agent == "calibrated":
        env = DummyVecEnv([lambda: eng])
        agent = agents.CalibratedAgent(env)
        agent.learn()
    elif args.agent == "ddpg":
        eng.symmetrize_actions()
        env = DummyVecEnv([lambda: eng])
        if args.use_pretrained:
            agent = DDPG.load(os.path.join(f"{args.agent}-pretrained", "agent"))
            agent.set_env(env)
            _, best_reward = utilities.evaluate_agent(DummyVecEnv([lambda: eng]), agent)
        else:
            n_actions = env.action_space.shape[-1]
            param_noise = None
            action_noise = OrnsteinUhlenbeckActionNoise(
                mean=np.zeros(n_actions), sigma=float(0.5) * np.ones(n_actions)
            )
            agent = DDPG(
                ddpgMlpPolicy,
                env,
                verbose=1,
                param_noise=param_noise,
                action_noise=action_noise,
                tensorboard_log=logdir,
            )
        agent.learn(total_timesteps=args.total_timesteps, callback=callback)
    elif args.agent == "a2c":
        env = SubprocVecEnv([lambda: eng for i in range(args.nranks)])
        if args.use_pretrained:
            agent = A2C.load(os.path.join(f"{args.agent}-pretrained", "agent"))
            agent.set_env(env)
        else:
            agent = A2C(MlpPolicy, env, verbose=1, n_steps=1, tensorboard_log=logdir)
        # agent.learn(total_timesteps=args.total_timesteps, callback=callback)
        agent.learn(total_timesteps=args.nep*args.nsteps, callback=callback)
    elif args.agent == "dqn":
        env = DummyVecEnv([lambda: eng])
        if args.use_pretrained:
            agent = DQN.load(os.path.join(f"{args.agent}-pretrained", "agent"), exploration_fraction=0.03, exploration_final_eps=0.02)
            agent.set_env(env)
            _, best_reward = utilities.evaluate_agent(DummyVecEnv([lambda: eng]), agent)
            agent.learn(total_timesteps=args.nep*args.nsteps, callback=callback)
        elif args.use_best_agent:
            agent = DQN.load(os.path.join(logdir, "best_agent.pkl"))
            agent.set_env(env)
            _, best_reward = utilities.evaluate_agent(DummyVecEnv([lambda: eng]), agent)
            agent.exploration_fraction = 1e-6
            agent.exploration_final_eps = 1e-6
            agent.learn(total_timesteps=args.nep*eng.nsteps, callback=callback, reset_num_timesteps=True)
        else:
            # agent = DQN(dqnMlpPolicy, env, verbose=1, tensorboard_log=logdir, exploration_fraction=0.05, exploration_final_eps=0.001)
            if (args.use_engine_0D):
                agent = DQN(CustomDQNPolicy, env, verbose=1, tensorboard_log=logdir, exploration_fraction=0.05, exploration_final_eps=0.001,
                        target_network_update_freq=eng.nsteps*2, learning_starts=eng.nsteps*2, learning_rate=1e-3, buffer_size=eng.nsteps*args.nep,
                        gamma=0.99)
                agent.learn(total_timesteps=args.nep*eng.nsteps, callback=callback)
            else:
                # agent = DQN(dqnMlpPolicy, env, verbose=1, tensorboard_log=logdir)
                agent = DQN(CustomDQNPolicy, env, verbose=1, tensorboard_log=logdir, exploration_fraction=0.1, exploration_final_eps=0.02,
                            learning_rate=3e-4, gamma=0.9, buffer_size=args.nsteps*args.nep, batch_size=128, prioritized_replay=True,
                            learning_starts=args.nsteps*10)
                agent.learn(total_timesteps=args.nep*args.nsteps, callback=callback)
    elif args.agent == "ppo":
        env = SubprocVecEnv([lambda: eng for i in range(args.nranks)])
        if args.use_pretrained:
            agent = PPO2.load(os.path.join(f"{args.agent}-pretrained", "agent"))
            agent.set_env(env)
        else:
            # agent = PPO2(MlpPolicy, env, verbose=1, n_steps=64, nminibatches=4, tensorboard_log=logdir, gamma=0.99)
            agent = PPO2(MlpPolicy,
            # agent = PPO2(MlpLnLstmPolicy,
                        env,
                        verbose=1,
                        gamma=0.998,
                        ent_coef=5.33e-3,
                        learning_rate=1.76e-5,
                        vf_coef=0.188,
                        max_grad_norm=0.88,
                        lam=0.99,
                        nminibatches=4,
                        noptepochs=16,
                        cliprange=0.68,
                        n_steps=16,
                        )
        if args.use_engine_0D:
            agent.learn(total_timesteps=args.nep*eng.nsteps*args.nranks, callback=callback)
        else:
            agent.learn(total_timesteps=args.nep*args.nsteps*args.nranks, callback=callback)

    # # Save, evaluate, and plot the agent
    pfx = os.path.join(logdir, "agent")
    agent.save(pfx)
    env = DummyVecEnv([lambda: eng])
    df, total_reward = utilities.evaluate_agent(env, agent)


    df.to_csv(pfx + ".csv", index=False)
    if (args.use_engine_0D):
        utilities.plot_df_0D(env, df, idx=0, name=args.agent)
    else:
        utilities.plot_df(env, df, idx=0, name=args.agent)
    utilities.save_plots(pfx + ".pdf")

    # Plot the training history
    logs = pd.read_csv(logname)
    utilities.plot_training(logs, os.path.join(logdir, "logger.pdf"))

    # output timer
    end = time.time() - start
    print(f"Elapsed time {timedelta(seconds=end)} (or {end} seconds)")
