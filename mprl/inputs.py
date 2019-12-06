import sys
import toml
import copy


# ========================================================================
#
# Classes
#
# ========================================================================
class Parameter:
    def __init__(self, default, helper, typer, choices=None):
        self.default = default
        self.helper = helper
        self.typer = typer
        self.choices = choices
        self.set_value(self.default)

    def __repr__(self):
        return "Parameter()"

    def __srt__(self):
        return "member of Parameter"

    def set_value(self, value):
        if type(value) == self.typer:
            self.value = value
        elif value is None:
            self.value = value
        else:
            sys.exit(
                f"Type does not match for {self.name} ({self.typer} is not {type(value)})"
            )

        if (self.choices is not None) and (value not in self.choices):
            sys.exit(
                f"Value {value} is not part of the available choices {self.choices}"
            )


# ========================================================================
class Input:
    def __init__(self):

        self.inputs = {
            "agent": {
                "agent": Parameter(
                    "ppo",
                    "Agent to train and evaluate",
                    str,
                    choices=["calibrated", "exhaustive", "ppo"],
                ),
                "number_episodes": Parameter(
                    100, "Total number of episodes to train over", int
                ),
                "update_nepisodes": Parameter(
                    20, "Number of episodes per agent update", int
                ),
                "nranks": Parameter(1, "Number of MPI ranks", int),
                "use_pretrained": Parameter(
                    None,
                    "Directory containing a pretrained network to use as a starting point",
                    str,
                ),
            },
            "engine": {
                "engine": Parameter(
                    "twozone-engine",
                    "Engine",
                    str,
                    choices=["twozone-engine", "reactor-engine", "EQ-engine"],
                ),
                "fuel": Parameter(
                    "dodecane", "Fuel", str, choices=["dodecane", "PRF100", "PRF85"]
                ),
                "rxnmech": Parameter(
                    "dodecane_lu_nox.cti",
                    "Reaction mechanism file",
                    str,
                    choices=[
                        "dodecane_lu_nox.cti",
                        "dodecane_mars.cti",
                        "dodecane_lu.cti",
                        "llnl_gasoline_surrogate_323.xml",
                    ],
                ),
                "observables": Parameter(
                    ["ca", "p", "T", "success_ninj", "can_inject"],
                    "Engine observables",
                    list,
                ),
                "nsteps": Parameter(101, "Engine steps in a given episode", int),
                "mdot": Parameter(0.1, "Injected mass flow rate [kg/s]", float),
                "max_minj": Parameter(5e-5, "Maximum fuel injected mass [kg]", float),
                "max_injections": Parameter(
                    None, "Maximum number of injections allowed", int
                ),
                "injection_delay": Parameter(
                    0.0, "Time delay between injections", float
                ),
                "negative_reward": Parameter(
                    -800.0, "Negative reward for unallowed actions", float
                ),
                "use_qdot": Parameter(False, "Use a Qdot as an action", bool),
                "use_continuous": Parameter(
                    False, "Use a continuous action space", bool
                ),
            },
        }

    def write_toml(self):
        """Write inputs as TOML format"""
        for section in self.inputs.keys():
            print(f"""[{section}]""")
            for name, param in self.inputs[section].items():
                if type(param.value) is str:
                    print(f"""{name} = "{param.value}" """)
                else:
                    print(f"""{name} = {param.value}""")

    def print_help(self):
        """Print the defaults and help"""
        for section in self.inputs.keys():
            print(f"""[{section}]""")
            for name, param in self.inputs[section].items():
                if type(param.value) is str:
                    print(f"""{name} = "{param.default}" # {param.helper}""")
                else:
                    print(f"""{name} = {param.default} # {param.helper}""")

    def from_toml(self, fname):
        """Read TOML file for inputs"""
        parsed = toml.load(fname)
        for section in parsed.keys():
            for key, value in parsed[section].items():
                self.inputs[section][key].set_value(value)
