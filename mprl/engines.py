# ========================================================================
#
# Imports
#
# ========================================================================
import os
import sys
import cantera as ct
import numpy as np
import pandas as pd
from scipy.integrate import ode
import gym
from gym import spaces
import mprl.utilities as utilities
import mprl.actiontypes as actiontypes


# ========================================================================
#
# Functions
#
# ========================================================================
def get_reward(state):
    return state.p * state.dV


# ========================================================================
def calibrated_engine_ic():
    T0 = 273.15 + 120
    p0 = 264_647.769_165_039_06
    return T0, p0


# ========================================================================
def setup_injection_gas(rxnmech, fuel, pure_fuel=True, phi=1.0):
    """Setup the injection gas"""
    gas = ct.Solution(rxnmech)
    far = 0.0

    if fuel == "PRF100":
        gas.set_equivalence_ratio(
            phi, {"IC8H18": 1.0, "NC7H16": 0.0}, {"O2": 1.0, "N2": 3.76}
        )
        afr = (gas.Y[gas.species_index("O2")] + gas.Y[gas.species_index("N2")]) / (
            gas.Y[gas.species_index("IC8H18")]
        )
        far = 1.0 / afr
    elif fuel == "PRF85":
        gas.set_equivalence_ratio(
            phi, {"IC8H18": 0.85, "NC7H16": 0.15}, {"O2": 1.0, "N2": 3.76}
        )
        afr = (gas.Y[gas.species_index("O2")] + gas.Y[gas.species_index("N2")]) / (
            gas.Y[gas.species_index("IC8H18")] + gas.Y[gas.species_index("NC7H16")]
        )
        far = 1.0 / afr
    elif fuel == "dodecane":
        if pure_fuel:
            gas.X = {"NC12H26": 1.0}
        else:
            gas.set_equivalence_ratio(phi, {"NC12H26": 1.0}, {"O2": 1.0, "N2": 3.76})
            afr = (
                gas.Y[gas.species_index("O2")] + gas.Y[gas.species_index("N2")]
            ) / gas.Y[gas.species_index("NC12H26")]
            far = 1.0 / afr
    else:
        sys.exit(f"Unrecognized fuel {fuel}")

    return gas, far


# ========================================================================
def get_nox(gas):
    try:
        no = gas.mass_fraction_dict()["NO"]
    except KeyError:
        no = 0.0
    try:
        no2 = gas.mass_fraction_dict()["NO2"]
    except KeyError:
        no2 = 0.0

    return no + no2


# ========================================================================
def get_soot(gas):
    try:
        return gas.mass_fraction_dict()["C2H2"]
    except KeyError:
        return 0.0


# ========================================================================
def get_observables_internals(other, histories, observables):
    valid_observables = other + histories
    if len(list(set(valid_observables) & set(observables))) == 0:
        sys.exit(
            f"Selected observables ({observables}) not in valid observables ({valid_observables})"
        )

    internals = list(set(other) - set(observables))
    return observables, internals


# ========================================================================
#
# Classes
#
# ========================================================================
class Engine(gym.Env):
    """An engine environment for OpenAI gym"""

    def __init__(
        self,
        agent_steps=100,
        ivc=-100.0,
        evo=100.0,
        fuel="dodecane",
        rxnmech="dodecane_lu_nox.cti",
        negative_reward=-800.0,
    ):
        super(Engine, self).__init__()

        # Engine parameters
        self.T0, self.p0 = calibrated_engine_ic()
        self.agent_steps = agent_steps
        self.nsteps = self.agent_steps
        self.ivc = ivc
        self.evo = evo
        self.fuel = fuel
        self.rxnmech = rxnmech
        self.Bore = 0.0860000029206276  # Bore (m)
        self.Stroke = 0.0860000029206276  # Stroke length (m)
        self.RPM = 1500  # RPM of the engine
        self.TDCvol = 6.09216205775738e-5  # Volume at Top-Dead-Center (m^3)
        self.s2ca = 1.0 / (60.0 / self.RPM / 360.0)
        self.total_time = (
            self.evo - self.ivc
        ) / self.s2ca  # Time take to complete (evo - ivc) rotation in seconds
        self.small_mass = 1.0e-15
        self.max_burned_mass = 6e-3
        self.max_pressure = 200 * ct.one_atm
        self.negative_reward = negative_reward * (1 / (self.nsteps - 1))
        self.nepisode = 0
        self.action = None
        self.state_updater = {}
        self.state_reseter = {}
        self.datadir = os.path.join(
            os.path.dirname(os.path.realpath(__file__)), "datafiles"
        )
        ct.add_directory(self.datadir)

        self.observable_space_lows = {
            "ca": self.ivc,
            "p": 0.0,
            "T": 0.0,
            "attempt_ninj": 0.0,
            "success_ninj": 0.0,
            "can_inject": 0,
        }
        self.observable_space_highs = {
            "ca": self.evo,
            "p": np.finfo(np.float32).max,
            "T": np.finfo(np.float32).max,
            "attempt_ninj": np.iinfo(np.int32).max,
            "success_ninj": np.iinfo(np.int32).max,
            "can_inject": 1,
        }
        self.observable_scales = {
            "ca": 0.5 * (self.evo - self.ivc),
            "p": ct.one_atm * 100,
            "T": 2000,
            "attempt_ninj": 1.0,
            "success_ninj": 1.0,
            "can_inject": 1,
        }

    def define_observable_space(self):
        """Define the observable space"""
        obs_low = np.zeros(len(self.observables))
        obs_high = np.zeros(len(self.observables))
        for k, observable in enumerate(self.observables):
            obs_low[k] = self.observable_space_lows[observable]
            obs_high[k] = self.observable_space_highs[observable]

        self.observation_space = spaces.Box(
            low=obs_low, high=obs_high, dtype=np.float32
        )

    def scale_observables(self, df):
        sdf = df.copy()
        for obs in self.observables:
            sdf[obs] /= self.observable_scales[obs]
        return sdf

    def setup_discrete_injection_actions(self):
        """Setup the discrete injection actions"""

        if self.max_injections is None:
            self.max_injections = np.int(
                np.rint(self.max_minj / (self.mdot * self.dt_agent))
            )
            print("Maximum number of injections is ", self.max_injections)
        else:
            print("Warning: engine setup is overwriting the default max_injections")

        self.action = actiontypes.DiscreteActionType(
            ["mdot"],
            scales={"mdot": self.mdot},
            limits={"mdot": self.max_injections},
            delays={"mdot": self.injection_delay / self.dt_agent},
        )
        self.action_space = self.action.space

    def setup_history(self):
        """Setup the engine history and save for faster reset"""
        cname = os.path.join(self.datadir, "Isooctane_MBT_DI_50C_Summ.xlsx")
        self.full_cycle = pd.concat(
            [
                pd.read_excel(
                    cname, sheet_name="Ensemble Average", usecols=["PCYL1 - [kPa]_1"]
                ),
                pd.read_excel(cname, sheet_name="Volume"),
            ],
            axis=1,
        )
        self.full_cycle.rename(
            index=str,
            columns={
                "Crank Angle [ATDC]": "ca",
                "Volume [Liter]": "V",
                "PCYL1 - [kPa]_1": "p",
                "dVolume [Liter]": "dV",
            },
            inplace=True,
        )
        l2m3 = 1e-3
        self.full_cycle.p = self.full_cycle.p * 1e3 + 101_325.0
        self.full_cycle.V = self.full_cycle.V * l2m3
        self.full_cycle["t"] = (self.full_cycle.ca + 360) / self.s2ca

        cycle = self.full_cycle[
            (self.full_cycle.ca >= self.ivc) & (self.full_cycle.ca <= self.evo)
        ]
        self.exact = cycle[["p", "ca", "t", "V"]].copy()

        # interpolate the cycle
        interp, self.dca = np.linspace(self.ivc, self.evo, self.nsteps, retstep=True)
        cycle = utilities.interpolate_df(interp, "ca", cycle)
        self.dt = self.dca / self.s2ca
        self.dt_agent = self.total_time / (self.agent_steps - 1)
        self.dca_agent = self.dt_agent * self.s2ca

        # Initialize the engine history
        self.history = pd.DataFrame(
            0.0, index=np.arange(len(cycle.index)), columns=self.histories
        )
        self.starting_cycle_p = cycle.p[0]
        self.history.V = cycle.V.copy()
        self.history.dV = np.gradient(self.history.V)
        self.history.dVdt = self.history.dV / self.dt
        self.history.ca = cycle.ca.copy()
        self.history.t = cycle.t.copy()

    def set_initial_state(self):
        self.p0 = self.starting_cycle_p
        self.T0 = (
            (self.p0 / ct.one_atm)
            * (
                self.history.V[0]
                / (np.pi / 4.0 * self.Bore ** 2 * self.Stroke + self.TDCvol)
            )
            * 300.0
        )

    def reset_state(self):
        """Reset the starting state"""
        self.set_initial_state()

        self.current_state = pd.Series(
            0.0,
            index=list(
                dict.fromkeys(self.histories + self.observables + self.internals)
            ),
            name=0,
        )

        self.current_state.p = self.p0
        self.current_state["T"] = self.T0
        self.current_state[self.histories] = self.history.loc[0, self.histories]

        for key, reseter in self.state_reseter.items():
            if key in self.current_state.index:
                self.current_state[key] = reseter()

    def update_state(self):
        """Update the state"""
        for key in self.current_state.index:
            self.current_state[key] = self.state_updater[key]()
        self.current_state.name += 1

    def termination(self):
        """Evaluate termination criteria"""

        done = False
        reward = get_reward(self.current_state)
        if self.current_state.name >= len(self.history) - 1:
            done = True
        elif self.current_state.p > self.max_pressure:
            print(f"Maximum pressure (p = {self.max_pressure}) has been exceeded!")
            reward = self.negative_reward

        return reward, done

    def render(self, mode="human", close=False):
        """Render the environment to the screen"""
        print("Nothing to render")


# ========================================================================
class TwoZoneEngine(Engine):
    """A two zone engine environment for OpenAI gym"""

    def __init__(self, *args, **kwargs):
        super(TwoZoneEngine, self).__init__(*args, **kwargs)

        # Engine parameters
        self.negative_reward = -self.agent_steps
        self.ode_state = ["p", "Tu", "Tb", "mb"]
        self.histories = ["V", "dVdt", "dV", "ca", "t"]
        self.integ = ode(lambda t, y: self.dfundt_mdot(t, y, 0, 0, 0))

        self.state_reseter = {
            "Tu": lambda: self.T0,
            "Tb": lambda: self.Tb_ad,
            "can_inject": lambda: 1,
        }

        self.state_updater = {
            "p": lambda: self.integ.y[0],
            "Tu": lambda: self.integ.y[1],
            "Tb": lambda: self.integ.y[2],
            "mb": lambda: self.integ.y[3],
            "T": lambda: self.current_state["T"],
            "V": lambda: self.history.loc[self.current_state.name + 1].V,
            "dVdt": lambda: self.history.loc[self.current_state.name + 1].dVdt,
            "dV": lambda: self.history.loc[self.current_state.name + 1].dV,
            "ca": lambda: self.history.loc[self.current_state.name + 1].ca,
            "t": lambda: self.history.loc[self.current_state.name + 1].t,
            "attempt_ninj": lambda: self.action.attempt_counter["mdot"],
            "success_ninj": lambda: self.action.success_counter["mdot"],
            "can_inject": lambda: 1 if self.action.isallowed()["mdot"] else 0,
        }

        # Engine setup
        self.setup_fuel()
        self.setup_history()

    def setup_fuel(self):
        """Setup the fuel and save for faster reset"""

        self.injection_gas, self.far = setup_injection_gas(
            self.rxnmech, self.fuel, pure_fuel=False
        )

        self.injection_gas.TP = self.T0, self.p0
        self.injection_xinit = self.injection_gas.X

        self.injection_gas.equilibrate("HP", solver="gibbs")
        self.injection_xburnt = self.injection_gas.X
        self.injection_Tb_ad = self.injection_gas.T

    def reset(self):
        """Reset fuel and oxidizer"""
        self.gas1 = self.injection_gas
        self.xinit = self.injection_xinit
        self.xburnt = self.injection_xburnt
        self.Tb_ad = self.injection_Tb_ad

        super(TwoZoneEngine, self).reset_state()

        self.action.reset()

        obs = self.scale_observables(self.current_state)[self.observables]
        return obs

    def step(self, action):
        """Advance the engine to the next state using the action"""

        action = self.action.preprocess(action)

        # Integrate the model using the action
        step = self.current_state.name
        self.integ = ode(
            lambda t, y: self.dfundt_mdot(
                t,
                y,
                action["mdot"],
                self.history.V.loc[step + 1],
                self.history.dVdt.loc[step + 1],
                Qdot=action["qdot"] if self.action.use_qdot else 0.0,
            )
        )
        self.integ.set_initial_value(
            self.current_state[self.ode_state], self.current_state.t
        )
        self.integ.set_integrator("vode", atol=1.0e-8, rtol=1.0e-4)
        self.integ.integrate(self.history.t.loc[step + 1])

        # Update state
        self.update_state()

        reward, done = self.termination()

        # Add negative reward if the action had to be masked
        if self.action.masked:
            reward += self.negative_reward

        if done:
            print(f"Finished episode #{self.nepisode}")
            self.nepisode += 1

        return (
            self.scale_observables(self.current_state)[self.observables],
            reward,
            done,
            {"current_state": self.current_state},
        )

    def dfundt_mdot(self, t, y, mxdot, V, dVdt, Qdot=0.0):
        """
        ODE defining the state evolution.

        :param t: time
        :param y: state, [p, Tu, Tb, mb]
        :param mxdot: rate of injected fuel mass (later converted to mass burning rate)
        :param V: volume
        :param dVdot: volume rate of change
        :param Qdot: heat exchange rate between the gases and the cylinder walls
        :returns: ODE

        The equations solved here come from:

        @article{VerhelstS09,
        Author = {S. Verhelst and C.G.W. Sheppard},
        Date-Added = {2016-08-26 14:41:32 +0000},
        Date-Modified = {2016-08-26 14:42:44 +0000},
        Doi = {doi:10.1016/j.enconman.2009.01.002},
        Journal = {Energy Conversion and Management},
        Pages = {1326--1335},
        Title = {Multi-zone thermodynamic modelling of spark-ignition engine combustion -- An overview},
        Volume = {50},
        Year = {2009}}

        The equations are A.21, A.24, A.26.

        In addition, we assume that ml_udot (leakage of unburned gas
        from cylinder to crankcase) is 0 and ml_bdot (leakage of
        burned gas from cylinder to crankcase) is 0.

        """

        p, Tu, Tb, mb = y

        # Compute with cantera burnt gas properties
        self.gas1.TPX = Tb, p, self.xburnt
        cv_b = self.gas1.cv
        ub = self.gas1.u  # internal energy
        Rb = 8314.47215 / self.gas1.mean_molecular_weight
        Vb = self.gas1.v * mb

        # Compute with cantera unburnt gas properties
        self.gas1.TPX = Tu, p, self.xinit
        cv_u = self.gas1.cv
        cp_u = self.gas1.cp
        uu = self.gas1.u
        Ru = 8314.47215 / self.gas1.mean_molecular_weight
        vu = self.gas1.v

        invgamma_u = cv_u / cp_u
        RuovRb = Ru / Rb

        # This compute based on unburned gas EOS, mb, p (get Vb,
        # then V-Vb, or mu and then Vu directly)
        Vu = V - Vb
        # Vu = np.maximum(Vu,self.small_mass)
        # if Vu < 0:
        # print("Volume is negative!!!")
        # exit()
        m_u = Vu / vu

        # Trim mass burning rate if there isn't any unburned gas left
        # if m_u < 1.0e-10:
        #    mbxdot = 0.0

        # Heat exchange rate between the unburned zone and the cylinder walls
        if mb >= self.small_mass:
            Qudot = 0.0
        else:
            Qudot = Qdot

        # Equation A.13, rate of change in the burned mass (get from mdot of fuel)
        mbxdot = mxdot * (1 + 1 / self.far)
        dmbdt = mbxdot

        # Equation A.26, rate of change of the cylinder pressure
        # There is a typo (missing Vu) in the paper (units wouldn't match)
        dpdt = (
            1.0
            / (invgamma_u * Vu - cv_b * RuovRb / cp_u * Vu + cv_b / Rb * V)
            * (
                -1.0 * (1 + cv_b / Rb) * p * dVdt
                - Qdot
                - ((ub - uu) - cv_b * (Tb - RuovRb * Tu)) * mbxdot
                + (cv_u / cp_u - cv_b / Rb * Ru / cp_u) * Qudot
            )
        )

        # Equation A.21, rate of change of unburned gas temperature
        dTudt = 1.0 / (m_u * cp_u) * (Vu * dpdt - Qudot)

        # Equation A.24
        if mb <= self.small_mass:
            dTbdt = 0.0
        else:
            dTbdt = (
                p
                / (mb * Rb)
                * (
                    dVdt
                    - (Vb / mb - Vu / m_u) * mbxdot
                    + V / p * dpdt
                    - Vu / Tu * dTudt
                )
            )

        self.current_state["T"] = (m_u * Tu + mb * Tb) / (m_u + mb)

        return np.array((dpdt, dTudt, dTbdt, dmbdt))


# ========================================================================
class ContinuousTwoZoneEngine(TwoZoneEngine):
    """A two zone engine environment for OpenAI gym

    Description:
        A two-zone model engine is controlled by injecting burned mass.

    Observation:
        Type: Box(4)
        Name   Observation                                Min         Max
        ca     Engine crank angle                         ivc deg     evo deg
        p      Engine pressure                            0           Inf
        T      Gas temperature                            0           Inf
        V      Engine volume                              0           Inf
        dVdt   Engine volume rate of change              -Inf         Inf

    Available actions:
        Type: Box(2)
        Name  Action                                                  Min        Max
        mdot  injection rate of burned mass                           0          max_mdot
        qdot  (optional) heat transfer rate to the cylinder walls    -max_qdot   max_qdot

    Reward:
        Reward is (p dV) for every step taken, including the termination step

    Starting State:
        Initial engine conditions

    Episode Termination:
        Engine reached evo crank angle
        Engine pressure is more than 80bar
        Total injected burned mass is greater than a specified max mass (6e-4 kg)
    """

    def __init__(self, *args, use_qdot=False, **kwargs):
        super(ContinuousTwoZoneEngine, self).__init__(*args, **kwargs)

        # Engine parameters
        self.observables, self.internals = get_observables_internals(
            ["p", "T", "Tu", "Tb", "mb"], self.histories, ["ca"]
        )

        # Final setup
        action_names = ["mdot", "qdot"] if use_qdot else ["mdot"]
        self.action = actiontypes.ContinuousActionType(action_names)
        self.action_space = self.action.space
        self.define_observable_space()
        self.reset()


# ========================================================================
class DiscreteTwoZoneEngine(TwoZoneEngine):
    """A two zone engine environment for OpenAI gym

    Description:
        A two-zone model engine is controlled by injecting discrete burned mass.

    Observation:
        Type: Box(4)
        Name           Observation                                Min         Max
        ca             Engine crank angle                         ivc deg     evo deg
        p              Engine pressure                            0           Inf
        T              Gas temperature                            0           Inf
        attempt_ninj   Attempted number of injections             0           Inf
        success_ninj   Successful number of injections            0           Inf
        V              Engine volume                              0           Inf
        dVdt           Engine volume rate of change              -Inf         Inf

    Available actions:
        Type: Discrete
        Name  Action
        mdot  injection rate of burned mass

    Reward:
        Reward is (p dV) for every step taken, including the termination step

    Starting State:
        Initial engine conditions

    Episode Termination:
        Engine reached evo crank angle
        Engine pressure is more than 80bar
        Total injected burned mass is greater than a specified max mass (6e-4 kg)
    """

    def __init__(
        self,
        *args,
        mdot=0.1,  # Rate of mass injection (kg/s)
        max_minj=5e-05,  # Maximum mass of injected burned fuel/air mixture (kg) allowed
        max_injections=None,  # Maximum number of injections allowed
        injection_delay=0,  # Time delay between injections (s)
        observables=["ca", "p", "T", "success_ninj", "can_inject"],
        **kwargs,
    ):
        super(DiscreteTwoZoneEngine, self).__init__(*args, **kwargs)

        # Engine parameters
        self.observables, self.internals = get_observables_internals(
            ["attempt_ninj", "success_ninj", "can_inject", "p", "T", "Tu", "Tb", "mb"],
            self.histories,
            observables,
        )
        self.mdot = mdot
        self.max_minj = max_minj
        self.max_injections = max_injections
        self.injection_delay = injection_delay

        # Final setup
        self.setup_discrete_injection_actions()
        self.define_observable_space()
        self.reset()

    def reset(self):

        super(DiscreteTwoZoneEngine, self).reset()

        obs = self.scale_observables(self.current_state)[self.observables]
        return obs


# ========================================================================
class ReactorEngine(Engine):
    """An engine environment for OpenAI gym

    Description:
        A 0D Cantera Reactor engine that injects a fixed composition of fuel/air mixture

    Observation:
        Type: Box(5)
        Name           Observation                                Min         Max
        ca             Engine crank angle                         ivc deg     evo deg
        p              Engine pressure                            0           Inf
        T              Gas temperature                            0           Inf
        attempt_ninj   Attempted number of injections             0           Inf
        success_ninj   Successful number of injections            0           Inf
        V              Engine volume                              0           Inf
        dVdt           Engine volume rate of change              -Inf         Inf

    Available actions:
        Type: Discrete
        Name       Action                           Min        Max
        injection  inject fuel                      0          1

    Reward:
        Reward is (p dV) for every step taken, including the termination step

    Starting State:
        Initial engine conditions

    Episode Termination:
        Engine reached evo crank angle
        Engine pressure is more than 200bar
    """

    metadata = {"render.modes": ["human"]}

    def __init__(
        self,
        *args,
        dt=4e-6,  # Time step for integrating the 0D reactor (s)
        Tinj=300.0,  # Injection temperature of fuel/air mixture (K)
        mdot=0.1,  # Rate of mass injections (kg/s)
        max_minj=5e-5,  # Mass of injected fuel/air mixture (kg)
        max_injections=None,  # Maximum number of injections allowed
        injection_delay=0,  # Time delay between injections (s)
        observables=["ca", "p", "T", "success_ninj", "can_inject"],
        **kwargs,
    ):
        super(ReactorEngine, self).__init__(*args, **kwargs)

        # Engine parameters
        self.Tinj = Tinj
        self.histories = ["V", "dVdt", "dV", "ca", "t", "piston_velocity"]
        self.observables, self.internals = get_observables_internals(
            [
                "attempt_ninj",
                "success_ninj",
                "can_inject",
                "p",
                "T",
                "mb",
                "minj",
                "nox",
                "soot",
            ],
            self.histories,
            observables,
        )
        self.mdot = mdot
        self.max_minj = max_minj
        self.max_injections = max_injections
        self.injection_delay = injection_delay

        self.state_reseter = {"can_inject": lambda: 1}

        self.state_updater = {
            "p": lambda: self.gas.P,
            "T": lambda: self.gas.T,
            "mb": lambda: 0,
            "minj": lambda: self.action.current["mdot"] * self.dt_agent,
            "nox": lambda: get_nox(self.gas),
            "soot": lambda: get_soot(self.gas),
            "V": lambda: self.history.loc[self.current_state.name + 1].V,
            "dVdt": lambda: self.history.loc[self.current_state.name + 1].dVdt,
            "dV": lambda: self.history.loc[self.current_state.name + 1].dV,
            "ca": lambda: self.history.loc[self.current_state.name + 1].ca,
            "t": lambda: self.history.loc[self.current_state.name + 1].t,
            "piston_velocity": lambda: self.history.loc[
                self.current_state.name + 1
            ].piston_velocity,
            "attempt_ninj": lambda: self.action.attempt_counter["mdot"],
            "success_ninj": lambda: self.action.success_counter["mdot"],
            "can_inject": lambda: 1 if self.action.isallowed()["mdot"] else 0,
        }

        # Figure out the subcycling of steps
        self.dt_agent = self.total_time / (self.agent_steps - 1)
        self.substeps = int(np.ceil(self.dt_agent / dt)) + 1
        self.nsteps = (self.agent_steps - 1) * (self.substeps - 1) + 1

        # Engine setup
        self.setup_history()
        self.set_initial_state()
        self.setup_engine()
        self.setup_discrete_injection_actions()
        self.define_observable_space()
        self.reset()

    def setup_engine(self):
        """Setup the fuel and reactor"""
        self.setup_piston()
        self.setup_reactor()

    def setup_piston(self):
        """Calculates the piston velocity given engine history"""
        cylinder_area = np.pi / 4.0 * self.Bore ** 2
        self.history.piston_velocity = self.history.dVdt / cylinder_area

    def setup_reactor(self):
        self.initial_gas = ct.Solution(self.rxnmech)
        self.initial_gas.TPX = self.T0, self.p0, {"O2": 0.21, "N2": 0.79}

        self.injection_gas, _ = setup_injection_gas(
            self.rxnmech, self.fuel, pure_fuel=True
        )

        # Create the reactor object
        self.gas = self.initial_gas
        self.reactor = ct.Reactor(self.gas)
        self.rempty = ct.Reactor(self.gas)

        # Set the initial states of the reactor
        self.reactor.chemistry_enabled = True
        self.reactor.volume = self.history.V[0]

        # Add in a wall that moves according to piston velocity
        self.piston = ct.Wall(
            left=self.reactor,
            right=self.rempty,
            A=np.pi / 4.0 * self.Bore ** 2,
            U=0.0,
            velocity=self.history.piston_velocity[0],
        )

        # Create the network object
        self.sim = ct.ReactorNet([self.reactor])

    def reset(self):

        super(ReactorEngine, self).reset_state()

        self.setup_reactor()
        self.sim.set_initial_time(self.current_state.t)

        self.action.reset()

        obs = self.scale_observables(self.current_state)[self.observables]
        return obs

    def step(self, action):
        "Advance the engine to the next state using the action"

        action = self.action.preprocess(action)

        # Integrate the model using the action
        reward = 0
        for substep in range(self.substeps - 1):
            step = self.current_state.name
            self.piston.set_velocity(self.current_state.piston_velocity)

            # inject only once per subcyling
            if action["mdot"] > 0 and substep == 0:
                minj = action["mdot"] * self.dt_agent
                m0 = self.gas.density_mass * self.reactor.volume
                Tnew = (m0 * self.gas.T + minj * self.Tinj) / (m0 + minj)
                Pnew = self.gas.P
                Xnew = (m0 * self.gas.X + minj * self.injection_gas.X) / (m0 + minj)

                self.gas = ct.Solution(self.rxnmech)
                self.gas.TPX = Tnew, Pnew, Xnew
                self.reactor = ct.Reactor(self.gas)
                self.reactor.chemistry_enabled = True
                self.reactor.volume = self.current_state.V
                self.piston = ct.Wall(
                    left=self.reactor,
                    right=self.rempty,
                    A=np.pi / 4.0 * self.Bore ** 2,
                    U=0.0,
                    velocity=self.current_state.piston_velocity,
                )

                self.sim = ct.ReactorNet([self.reactor])
                self.sim.set_initial_time(self.current_state.t)

            self.sim.advance(self.history.loc[step + 1, "t"])

            # Update state
            self.update_state()

            sub_reward, done = self.termination()

            reward += sub_reward

        # Add negative reward if the action had to be masked
        if self.action.masked:
            reward += self.negative_reward

        if done:
            print(f"Finished episode #{self.nepisode}")
            self.nepisode += 1

        return (
            self.scale_observables(self.current_state)[self.observables],
            reward,
            done,
            {"current_state": self.current_state},
        )


# ========================================================================
class EquilibrateEngine(Engine):
    def __init__(
        self,
        *args,
        Tinj=300.0,  # Injection temperature of fuel/air mixture (K)
        mdot=0.1,  # Rate of mass injections (kg/s)
        max_minj=5e-5,  # Mass of injected fuel (kg)
        max_injections=None,  # Maximum number of injections allowed
        injection_delay=0,  # Time delay between injections (s)
        observables=["ca", "p", "T", "success_ninj", "can_inject"],
        **kwargs,
    ):
        super(EquilibrateEngine, self).__init__(*args, **kwargs)

        # Engine parameters
        self.Tinj = Tinj
        self.histories = ["V", "dVdt", "dV", "ca", "t", "piston_velocity"]
        self.observables, self.internals = get_observables_internals(
            [
                "attempt_ninj",
                "success_ninj",
                "can_inject",
                "p",
                "T",
                "mb",
                "minj",
                "nox",
                "soot",
            ],
            self.histories,
            observables,
        )

        self.mdot = mdot
        self.max_minj = max_minj
        self.max_injections = max_injections
        self.injection_delay = injection_delay

        self.state_reseter = {"can_inject": lambda: 1}

        self.state_updater = {
            "p": lambda: self.gas.P,
            "T": lambda: self.gas.T,
            "mb": lambda: 0,
            "minj": lambda: self.action.current["mdot"] * self.dt_agent,
            "nox": lambda: get_nox(self.gas),
            "soot": lambda: get_soot(self.gas),
            "V": lambda: self.history.loc[self.current_state.name + 1].V,
            "dVdt": lambda: self.history.loc[self.current_state.name + 1].dVdt,
            "dV": lambda: self.history.loc[self.current_state.name + 1].dV,
            "ca": lambda: self.history.loc[self.current_state.name + 1].ca,
            "t": lambda: self.history.loc[self.current_state.name + 1].t,
            "piston_velocity": lambda: self.history.loc[
                self.current_state.name + 1
            ].piston_velocity,
            "attempt_ninj": lambda: self.action.attempt_counter["mdot"],
            "success_ninj": lambda: self.action.success_counter["mdot"],
            "can_inject": lambda: 1 if self.action.isallowed()["mdot"] else 0,
        }

        # Engine setup
        self.setup_history()
        self.set_initial_state()
        self.setup_gas()
        self.setup_discrete_injection_actions()
        self.define_observable_space()
        self.reset()

    def setup_gas(self):
        self.initial_gas = ct.Solution(self.rxnmech)
        self.initial_gas.TPX = self.T0, self.p0, {"O2": 0.21, "N2": 0.79}
        self.injection_gas, _ = setup_injection_gas(
            self.rxnmech, self.fuel, pure_fuel=True
        )
        self.gas = self.initial_gas

    def reset(self):
        super(EquilibrateEngine, self).reset_state()

        self.setup_gas()
        self.action.reset()
        obs = self.scale_observables(self.current_state)[self.observables]
        return obs

    def step(self, action):
        """Advance the engine to the next state using the action"""

        action = self.action.preprocess(action)

        # Integrate the model using the action
        step = self.current_state.name

        gamma = self.gas.cp / self.gas.cv

        P1 = self.gas.P
        V1 = self.history.V[step]
        V2 = self.history.V[step + 1]
        P2 = P1 / ((V2 / V1) ** gamma)
        T2 = P2 * V2 / (self.gas.density_mole * V1 * ct.gas_constant)
        self.gas.TP = T2, P2

        if action["mdot"] > 0:
            minj = action["mdot"] * self.dt_agent
            m0 = self.gas.density_mass * self.current_state.V
            Tnew = (m0 * self.gas.T + minj * self.Tinj) / (m0 + minj)
            Pnew = self.gas.P
            Xnew = (m0 * self.gas.X + minj * self.injection_gas.X) / (m0 + minj)

            self.gas = ct.Solution(self.rxnmech)
            self.gas.TPX = Tnew, Pnew, Xnew
            self.gas.equilibrate("UV", solver="auto", rtol=1e-9)

        self.update_state()

        reward, done = self.termination()

        # Add negative reward if the action had to be masked
        if self.action.masked:
            reward += self.negative_reward

        if done:
            print(f"Finished episode #{self.nepisode}")
            self.nepisode += 1

        return (
            self.scale_observables(self.current_state)[self.observables],
            reward,
            done,
            {"current_state": self.current_state},
        )
