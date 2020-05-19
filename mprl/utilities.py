# ========================================================================
#
# Imports
#
# ========================================================================
import itertools
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
from matplotlib.backends.backend_pdf import PdfPages
from matplotlib import rcParams
from scipy import interpolate as interp
import mprl.engines as engines
import os


# ========================================================================
#
# Some defaults variables
#
# ========================================================================
plt.rc("text", usetex=True)
cmap_med = [
    "#F15A60",
    "#7AC36A",
    "#5A9BD4",
    "#FAA75B",
    "#9E67AB",
    "#CE7058",
    "#D77FB4",
    "#737373",
]
cmap = [
    "#EE2E2F",
    "#008C48",
    "#185AA9",
    "#F47D23",
    "#662C91",
    "#A21D21",
    "#B43894",
    "#010202",
]
dashseq = [
    (None, None),
    [10, 5],
    [10, 4, 3, 4],
    [3, 3],
    [10, 4, 3, 4, 3, 4],
    [3, 3],
    [3, 3],
]
markertype = ["s", "d", "o", "p", "h"]
rcParams.update({"figure.autolayout": True, "figure.max_open_warning": 0})


# ========================================================================
#
# Functions
#
# ========================================================================
def get_label(name):
    labels = {
        "calibrated": "Calibrated",
        "exhaustive": "Exhaustive",
        "ddpg": "DDPG",
        "a2c": "A2C",
        "dqn": "DQN",
        "ppo": "PPO2",
        "reactor": "ReactorEngine",
        "EQ": "EQEngine",
        "discrete": "DiscreteTwoZone",
    }
    if name in labels:
        return labels[name]
    else:
        return name


# ========================================================================
def get_fields():
    return {
        "mdot": r"$\dot{m}~[\mathrm{kg/s}]$",
        "rewards": r"$r$",
        "T": r"$T~[\mathrm{K}]$",
        "phi": r"$\phi$",
        "Tu": r"$T_u~[\mathrm{K}]$",
        "Tb": r"$T_b~[\mathrm{K}]$",
        "m": r"$m~[\mathrm{kg}]$",
        "mb": r"$m_b~[\mathrm{kg}]$",
        "minj": r"$m_i~[\mathrm{kg}]$",
        "qdot": r"$\dot{Q}~[\mathrm{J/s}]$",
        "nox": r"$m_{NO_x}~[\mathrm{kg}]$",
        "soot": r"$m_{C_2 H_2}~[\mathrm{kg}]$",
        "attempt_ninj": r"attempted \# injections",
        "success_ninj": r"successful \# injections",
        "w_work": r"$\omega_{w}$",
        "w_nox": r"$\omega_{Y_{NO_x}}$",
        "w_soot": r"$\omega_{Y_{C_2 H_2}}$",
        "w_penalty": r"$\omega_p$",
        "r_work": r"$r_{w}$",
        "r_nox": r"$r_{Y_{NO_x}}$",
        "r_soot": r"$r_{Y_{C_2 H_2}}$",
        "r_penalty": r"$r_p$",
        "cumulative_rewards": r"$\Sigma_{t=0}^{N} r_t$",
        "cumulative_r_work": r"$\Sigma_{t=0}^{N} r_{w,t}$",
        "cumulative_r_nox": r"$\Sigma_{t=0}^{N} r_{Y_{NO_x,t}}$",
        "cumulative_r_soot": r"$\Sigma_{t=0}^{N} r_{Y_{C_2 H_2,t}}$",
        "cumulative_r_penalty": r"$\Sigma_{t=0}^{N} r_{p,t}$",
    }


# ========================================================================
def interpolate_df(x, name, fp):
    """Interpolate a dataframe

    :param x: the x-coordinates at which to evaluate the interpolated values
    :type x: array
    :param name: the name of the column to use in the dataframe for the x-coordinate
    :type name: str
    :param fp: the dataframe containing the y-coordinates
    :type fp: DataFrame
    :returns: the interpolated dataframe
    :rtype: DataFrame
    """
    df = pd.DataFrame({name: x})
    for col in fp.columns:
        f = interp.interp1d(fp[name], fp[col], kind="linear", fill_value="extrapolate")
        df[col] = f(df[name])

    return df


# ========================================================================
def evaluate_agent(env, agent):
    """Evaluate an agent in an engine environment.

    :param env: engine environment
    :type env: Environment
    :param agent: agent
    :type agent: Agent
    :returns: dataframe of history, total rewards
    """

    eng = env.envs[0]
    variables = eng.observables + eng.internals + eng.histories
    df = pd.DataFrame(
        columns=list(
            dict.fromkeys(
                variables + eng.action.actions + ["rewards"] + eng.reward.get_rewards()
            )
        )
    )

    # Evaluate actions from the agent in the environment
    done = False
    cnt = 0
    obs = env.reset()
    df.loc[cnt, variables] = [eng.current_state[k] for k in variables]
    df.loc[cnt, eng.action.actions] = 0
    rwd = list(eng.reward.compute(eng.current_state, eng.nsteps, False, False).values())
    df.loc[cnt, eng.reward.get_rewards()] = rwd
    df.loc[cnt, ["rewards"]] = [sum(rwd)]

    while not done:
        cnt += 1
        action, _ = agent.predict(obs, deterministic=True)
        obs, reward, done, info = env.step(action)
        df.loc[cnt, variables] = [info[0]["current_state"][k] for k in variables]
        df.loc[cnt, eng.action.actions] = eng.action.current
        df.loc[cnt, ["rewards"]] = reward
        df.loc[cnt, eng.reward.get_rewards()] = list(info[0]["rewards"].values())
        if df.loc[cnt, "mdot"] > 0:
            print(f"""Injecting at ca = {df.loc[cnt, "ca"]}""")

    for rwd in eng.reward.get_rewards() + ["rewards"]:
        df[f"cumulative_{rwd}"] = np.cumsum(df[rwd])

    return df, df.rewards.sum()


# ========================================================================
def plot_df(env, df, idx=0, name=None, plot_exp=True):
    """Make some plots of the agent performance"""

    eng = env.envs[0]
    pa2bar = 1e-5
    label = get_label(name)

    cidx = np.mod(idx, len(cmap))
    didx = np.mod(idx, len(dashseq))

    plt.figure("p")
    _, labels = plt.gca().get_legend_handles_labels()
    if "Exp." not in labels and plot_exp:
        plt.plot(eng.exact.ca, eng.exact.p * pa2bar, color=cmap[-1], lw=1, label="Exp.")
    p = plt.plot(df.ca, df.p * pa2bar, color=cmap[cidx], lw=2, label=label)
    p[0].set_dashes(dashseq[didx])

    plt.figure("p_v")
    _, labels = plt.gca().get_legend_handles_labels()
    if "Exp." not in labels and plot_exp:
        plt.plot(eng.exact.V, eng.exact.p * pa2bar, color=cmap[-1], lw=1, label="Exp.")
    p = plt.plot(df.V, df.p * pa2bar, color=cmap[cidx], lw=2, label=label)
    p[0].set_dashes(dashseq[didx])

    for field in get_fields():
        if field in df.columns:
            plt.figure(field)
            p = plt.plot(df.ca, df[field], color=cmap[cidx], lw=2, label=label)
            p[0].set_dashes(dashseq[didx])

    if plt.fignum_exists("phi") and plt.fignum_exists("T"):
        plt.figure("phi_temp")
        plt.plot(df["T"], df["phi"], "*--", color=cmap[cidx], lw=2, label=label)


# ========================================================================
def save_plots(fname):
    """Save plots"""

    if plt.fignum_exists("phi_temp"):
        datadir = os.path.join(
            os.path.dirname(os.path.realpath(__file__)),
            "datafiles",
            "NOx_soot_dodecane_lu_nox.npz",
        )
        data = np.load(datadir)

        NOx = data["NOx"]
        phi = data["phi"]
        temp = data["temp"]

    with PdfPages(fname) as pdf:

        plt.figure("p")
        ax = plt.gca()
        plt.xlabel(r"$\theta$", fontsize=22, fontweight="bold")
        plt.ylabel(r"$p~[\mathrm{bar}]$", fontsize=22, fontweight="bold")
        plt.setp(ax.get_xmajorticklabels(), fontsize=16)
        plt.setp(ax.get_ymajorticklabels(), fontsize=16)
        legend = ax.legend(loc="best")
        pdf.savefig(dpi=300)

        plt.figure("p_v")
        ax = plt.gca()
        plt.xlabel(r"$V$", fontsize=22, fontweight="bold")
        plt.ylabel(r"$p~[\mathrm{bar}]$", fontsize=22, fontweight="bold")
        plt.setp(ax.get_xmajorticklabels(), fontsize=16)
        plt.setp(ax.get_ymajorticklabels(), fontsize=16)
        # legend = ax.legend(loc="best")
        pdf.savefig(dpi=300)

        for field, label in get_fields().items():
            if plt.fignum_exists(field):
                plt.figure(field)
                ax = plt.gca()
                plt.xlabel(r"$\theta$", fontsize=22, fontweight="bold")
                plt.ylabel(label, fontsize=22, fontweight="bold")
                plt.setp(ax.get_xmajorticklabels(), fontsize=16)
                plt.setp(ax.get_ymajorticklabels(), fontsize=16)
                # legend = ax.legend(loc="best")
                pdf.savefig(dpi=300)

        if plt.fignum_exists("phi_temp"):
            fig = plt.figure("phi_temp")
            CM = plt.pcolormesh(
                temp, phi, NOx / NOx.max(axis=1).max(axis=0), cmap="hot"
            )
            plt.clim(0, 1)
            ax = plt.gca()
            if len(fig.axes) == 1:
                cbar = plt.colorbar(CM)
                cbar.set_label(r"Normalized Y(NO$_x$)")
            plt.xlabel(r"Temperature (K)", fontsize=22, fontweight="bold")
            plt.ylabel(r"$\phi$", fontsize=22, fontweight="bold")
            plt.setp(ax.get_xmajorticklabels(), fontsize=16)
            plt.setp(ax.get_ymajorticklabels(), fontsize=16)
            ax.set_xlim([500, 3000])
            ax.set_ylim([0, 1.0])
            legend = ax.legend(loc="best")
            pdf.savefig(dpi=300)


# ========================================================================
def plot_training(df, fname):
    """Make some plots of the training"""

    idx = 0

    cidx = np.mod(idx, len(cmap))
    didx = np.mod(idx, len(dashseq))
    midx = np.mod(idx, len(markertype))

    plt.figure("episode_reward")
    p = plt.plot(df.episode, df.episode_reward, color=cmap[cidx], lw=2)
    p[0].set_dashes(dashseq[didx])

    plt.figure("episode_step")
    p = plt.plot(df.episode, df.episode_step, color=cmap[cidx], lw=2)
    p[0].set_dashes(dashseq[didx])

    plt.figure("step_rewards")
    plt.scatter(
        df.episode_step,
        df.episode_reward,
        c=cmap[cidx],
        alpha=0.2,
        s=15,
        marker=markertype[midx],
    )

    with PdfPages(fname) as pdf:
        plt.figure("episode_reward")
        ax = plt.gca()
        plt.xlabel(r"episode", fontsize=22, fontweight="bold")
        plt.ylabel(r"$\Sigma r$", fontsize=22, fontweight="bold")
        plt.setp(ax.get_xmajorticklabels(), fontsize=16)
        plt.setp(ax.get_ymajorticklabels(), fontsize=16)
        # legend = ax.legend(loc="best")
        pdf.savefig(dpi=300)

        plt.figure("episode_step")
        ax = plt.gca()
        plt.xlabel(r"episode", fontsize=22, fontweight="bold")
        plt.ylabel(r"step", fontsize=22, fontweight="bold")
        plt.setp(ax.get_xmajorticklabels(), fontsize=16)
        plt.setp(ax.get_ymajorticklabels(), fontsize=16)
        # legend = ax.legend(loc="best")
        pdf.savefig(dpi=300)

        plt.figure("step_rewards")
        ax = plt.gca()
        plt.xlabel(r"step", fontsize=22, fontweight="bold")
        plt.ylabel(r"$\Sigma r$", fontsize=22, fontweight="bold")
        plt.setp(ax.get_xmajorticklabels(), fontsize=16)
        plt.setp(ax.get_ymajorticklabels(), fontsize=16)
        # legend = ax.legend(loc="best")
        pdf.savefig(dpi=300)


# ========================================================================
def plot_tb(fname, alpha=0.1, idx=0, name=None, limit=np.finfo(float).max):
    """Make some plots of tensorboard quantities"""

    label = get_label(name)
    df = pd.read_csv(fname)
    df["episode"] = df.step / 100  # 100 steps per episode
    df = df[df.episode <= limit]
    print(f"""Total training time for {fname}: {df.time.max():.2f} s""")

    cidx = np.mod(idx, len(cmap))
    didx = np.mod(idx, len(dashseq))

    subdf = df.dropna(subset=["episode_reward"])
    ewma = subdf["episode_reward"].ewm(alpha=alpha, adjust=False).mean()

    plt.figure("episode_reward")
    p = plt.plot(subdf.episode, subdf.episode_reward, color=cmap[cidx], lw=2, alpha=0.2)
    p[0].set_dashes(dashseq[didx])
    p = plt.plot(subdf.episode, ewma, color=cmap[cidx], lw=2, label=label)
    p[0].set_dashes(dashseq[didx])

    plt.figure("episode_reward_vs_time")
    p = plt.plot(subdf.time, subdf.episode_reward, color=cmap[cidx], lw=2, alpha=0.2)
    p[0].set_dashes(dashseq[didx])
    p = plt.plot(subdf.time, ewma, color=cmap[cidx], lw=2, label=label)
    p[0].set_dashes(dashseq[didx])

    subdf = df.dropna(subset=["loss"])
    plt.figure("loss")
    p = plt.plot(subdf.episode, subdf.loss, color=cmap[cidx], lw=2, alpha=0.2)
    p[0].set_dashes(dashseq[didx])
    ewma = subdf["loss"].ewm(alpha=alpha, adjust=False).mean()
    p = plt.plot(subdf.episode, ewma, color=cmap[cidx], lw=2, label=label)
    p[0].set_dashes(dashseq[didx])


# ========================================================================
def save_tb_plots(fname):
    """Make some plots of tensorboard quantities"""

    with PdfPages(fname) as pdf:
        plt.figure("episode_reward")
        ax = plt.gca()
        plt.xlabel(r"episode", fontsize=22, fontweight="bold")
        plt.ylabel(r"$\Sigma_{t=0}^{N} r_t$", fontsize=22, fontweight="bold")
        ax.set_xticklabels([f"{int(x/1000)}K" for x in ax.get_xticks().tolist()])
        plt.setp(ax.get_xmajorticklabels(), fontsize=16)
        plt.setp(ax.get_ymajorticklabels(), fontsize=16)
        # legend = ax.legend(loc="best")
        pdf.savefig(dpi=300)

        plt.figure("episode_reward_vs_time")
        ax = plt.gca()
        plt.xlabel(r"$t~[s]$", fontsize=22, fontweight="bold")
        plt.ylabel(r"$\Sigma_{t=0}^{N} r_t$", fontsize=22, fontweight="bold")
        plt.setp(ax.get_xmajorticklabels(), fontsize=16)
        plt.setp(ax.get_ymajorticklabels(), fontsize=16)
        # legend = ax.legend(loc="best")
        pdf.savefig(dpi=300)

        plt.figure("loss")
        ax = plt.gca()
        plt.xlabel(r"episode", fontsize=22, fontweight="bold")
        plt.ylabel(r"$L_t$", fontsize=22, fontweight="bold")
        ax.set_xticklabels([f"{int(x/1000)}K" for x in ax.get_xticks().tolist()])
        plt.setp(ax.get_xmajorticklabels(), fontsize=16)
        plt.setp(ax.get_ymajorticklabels(), fontsize=16)
        plt.ylim([1e-3, 1e5])
        plt.yscale("log")
        legend = ax.legend(loc="best")
        pdf.savefig(dpi=300)


# ========================================================================
def grouper(iterable, n):
    """Group iterable in chunks of n"""
    it = iter(iterable)
    while True:
        chunk = tuple(itertools.islice(it, n))
        if not chunk:
            return
        yield chunk
