from collections import OrderedDict
from enum import IntEnum

import numpy as np
import jax
import jax.numpy as jnp
from jax import lax
from jaxmarl.environments import MultiAgentEnv
from jaxmarl.environments import spaces
from typing import Tuple, Dict
import chex
from flax import struct
from flax.core.frozen_dict import FrozenDict
from jaxmarl.environments.overcooked_v2.common import (
    StaticObject,
    DynamicObject,
    Direction,
    Position,
    Agent,
)


from jaxmarl.environments.overcooked_v2.layouts import overcooked_layouts as layouts
from jaxmarl.environments.overcooked_v2.utils import tree_select


class Actions(IntEnum):
    # Turn left, turn right, move forward
    right = 0
    down = 1
    left = 2
    up = 3
    stay = 4
    interact = 5
    # done = 6


ACTION_TO_DIRECTION = jnp.full((len(Actions),), -1)
ACTION_TO_DIRECTION = ACTION_TO_DIRECTION.at[Actions.right].set(Direction.RIGHT)
ACTION_TO_DIRECTION = ACTION_TO_DIRECTION.at[Actions.down].set(Direction.DOWN)
ACTION_TO_DIRECTION = ACTION_TO_DIRECTION.at[Actions.left].set(Direction.LEFT)
ACTION_TO_DIRECTION = ACTION_TO_DIRECTION.at[Actions.up].set(Direction.UP)


@chex.dataclass
class State:
    agents: Agent

    # width x height x 3
    # First channel: static items
    # Second channel: dynamic items (plates and ingredients)
    # Third channel: extra info
    grid: chex.Array

    time: chex.Array
    terminal: bool


URGENCY_CUTOFF = 40  # When this many time steps remain, the urgency layer is flipped on
DELIVERY_REWARD = 20
POT_COOK_TIME = 20  # Time it takes to cook a pot of onions


class Overcooked(MultiAgentEnv):
    """Vanilla Overcooked"""

    def __init__(
        self,
        layout=layouts["counter_circuit"],
        # random_reset: bool = False,
        max_steps: int = 400,
    ):
        # Sets self.num_agents to 2
        super().__init__(num_agents=2)

        # self.obs_shape = (agent_view_size, agent_view_size, 3)
        # Observations given by 26 channels, most of which are boolean masks
        self.height = layout.height
        self.width = layout.width
        # self.obs_shape = (420,)
        self.obs_shape = (self.width, self.height, 3)

        self.agent_view_size = (
            5  # Hard coded. Only affects map padding -- not observations.
        )
        self.layout = layout
        self.agents = ["agent_0", "agent_1"]

        self.action_set = jnp.array(list(Actions))

        # self.random_reset = random_reset
        self.max_steps = max_steps

    def step_env(
        self,
        key: chex.PRNGKey,
        state: State,
        actions: Dict[str, chex.Array],
    ) -> Tuple[Dict[str, chex.Array], State, Dict[str, float], Dict[str, bool], Dict]:
        """Perform single timestep state transition."""

        acts = self.action_set.take(
            indices=jnp.array([actions["agent_0"], actions["agent_1"]])
        )

        state, reward = self.step_agents(key, state, acts)

        state = state.replace(time=state.time + 1)

        done = self.is_terminal(state)
        state = state.replace(terminal=done)

        obs = self.get_obs(state)

        rewards = {"agent_0": reward, "agent_1": reward}
        dones = {"agent_0": done, "agent_1": done, "__all__": done}

        return (
            lax.stop_gradient(obs),
            lax.stop_gradient(state),
            rewards,
            dones,
            {},
        )

    def reset(
        self,
        key: chex.PRNGKey,
    ) -> Tuple[Dict[str, chex.Array], State]:
        layout = self.layout

        static_objects = layout.static_objects
        grid = jnp.stack(
            [
                static_objects,
                jnp.zeros_like(static_objects),  # ingredient channel
                jnp.zeros_like(static_objects),  # extra info channel
            ],
            axis=-1,
        )

        num_agents = self.num_agents
        positions = layout.agent_positions
        agents = Agent(
            pos=Position(x=jnp.array(positions[:, 0]), y=jnp.array(positions[:, 1])),
            dir=jnp.full((num_agents,), Direction.UP),
            inventory=jnp.zeros((num_agents,), dtype=jnp.int32),
        )

        state = State(
            agents=agents,
            grid=grid,
            time=0,
            terminal=False,
        )

        obs = self.get_obs(state)

        return lax.stop_gradient(obs), lax.stop_gradient(state)

    def get_obs(self, state: State) -> Dict[str, chex.Array]:
        """
        Return a full observation, of size (height x width x 3)

        First channel contains static Items such as walls, pots, goal, plate_pile and ingredient_piles
        Second channel contains dynamic Items such as plates, ingredients and dishes
        Third channel contains agent positions and orientations
        """

        agents = state.agents
        obs = state.grid

        def _include_agents(grid, agent):
            pos = agent.pos
            inventory = agent.inventory
            direction = agent.dir
            return (
                grid.at[pos.y, pos.x].set([StaticObject.AGENT, inventory, direction]),
                None,
            )

        obs, _ = jax.lax.scan(_include_agents, obs, agents)

        def _agent_obs(agent):
            pos = agent.pos
            return obs.at[pos.y, pos.x, 0].set(StaticObject.SELF_AGENT)

        all_obs = jax.vmap(_agent_obs)(agents)

        obs_dict = {f"agent_{i}": obs for i, obs in enumerate(all_obs)}
        return obs_dict

    def step_agents(
        self,
        key: chex.PRNGKey,
        state: State,
        actions: chex.Array,
    ) -> Tuple[State, float]:
        grid = state.grid

        print("actions: ", actions)

        # Move action:
        # 1. move agent to new position (if possible on the grid)
        # 2. resolve collisions
        # 3. prevent swapping
        def _move_wrapper(agent, action):
            direction = ACTION_TO_DIRECTION[action]

            def _move(agent, dir):
                pos = agent.pos
                new_pos = pos.move_in_bounds(dir, self.width, self.height)

                new_pos = tree_select(
                    grid[new_pos.y, new_pos.x, 0] == StaticObject.EMPTY, new_pos, pos
                )

                return agent.replace(pos=new_pos, dir=direction)

            return jax.lax.cond(
                direction != -1,
                _move,
                lambda a, _: a,
                agent,
                direction,
            )

        new_agents = jax.vmap(_move_wrapper)(state.agents, actions)

        # Resolve collisions:
        def _resolved_positions(mask):
            return tree_select(mask, state.agents.pos, new_agents.pos)

        def _get_collisions(mask):
            positions = _resolved_positions(mask)

            collision_grid = jnp.zeros((self.height, self.width))
            collision_grid, _ = jax.lax.scan(
                lambda grid, pos: (grid.at[pos.y, pos.x].add(1), None),
                collision_grid,
                positions,
            )

            collision_mask = collision_grid > 1

            collisions = jax.vmap(lambda p: collision_mask[p.y, p.x])(positions)
            return collisions

        initial_mask = jnp.zeros((self.num_agents,), dtype=bool)
        mask = jax.lax.while_loop(
            lambda mask: jnp.any(_get_collisions(mask)),
            lambda mask: mask | _get_collisions(mask),
            initial_mask,
        )
        new_agents = new_agents.replace(pos=_resolved_positions(mask))

        # Prevent swapping:
        # TODO: implement this

        # Interact action:
        def _interact_wrapper(carry, x):
            agent, action = x
            is_interact = action == Actions.interact

            def _interact(carry, agent):
                grid, reward = carry

                print("interact: ", agent.pos, agent.dir)

                new_grid, new_agent, interact_reward = self.process_interact(
                    grid, agent
                )

                carry = (new_grid, reward + interact_reward)
                return carry, new_agent

            return jax.lax.cond(
                is_interact, _interact, lambda c, a: (c, a), carry, agent
            )

        carry = (grid, 0.0)
        xs = (new_agents, actions)
        (new_grid, reward), new_agents = jax.lax.scan(_interact_wrapper, carry, xs)

        # Cook pots:
        def _cook_wrapper(cell):
            is_pot = cell[0] == StaticObject.POT

            def _cook(cell):
                is_cooking = cell[2] > 0
                new_extra = jax.lax.select(is_cooking, cell[2] - 1, cell[2])
                finished_cooking = is_cooking * (new_extra == 0)
                new_ingredients = cell[1] | (finished_cooking * DynamicObject.COOKED)

                return jnp.array([cell[0], new_ingredients, new_extra])

            return jax.lax.cond(is_pot, _cook, lambda x: x, cell)

        new_grid = jax.vmap(jax.vmap(_cook_wrapper))(new_grid)

        return (
            state.replace(
                agents=new_agents,
                grid=new_grid,
            ),
            reward,
        )

    def process_interact(
        self,
        grid: chex.Array,
        agent: Agent,
    ):
        """Assume agent took interact actions. Result depends on what agent is facing and what it is holding."""

        inventory = agent.inventory
        fwd_pos = agent.get_fwd_pos()

        interact_cell = grid[fwd_pos.y, fwd_pos.x]

        interact_item = interact_cell[0]
        interact_ingredients = interact_cell[1]
        interact_extra = interact_cell[2]

        # Booleans depending on what the object is
        object_is_plate_pile = jnp.array(interact_item == StaticObject.PLATE_PILE)
        object_is_ingredient_pile = jnp.array(
            StaticObject.is_ingredient_pile(interact_item)
        )
        object_is_pile = object_is_plate_pile | object_is_ingredient_pile
        object_is_pot = jnp.array(interact_item == StaticObject.POT)
        object_is_goal = jnp.array(interact_item == StaticObject.GOAL)
        object_is_wall = jnp.array(interact_item == StaticObject.WALL)

        object_has_no_ingredients = jnp.array(interact_ingredients == 0)

        inventory_is_empty = inventory == 0
        inventory_is_ingredient = (inventory & DynamicObject.PLATE) == 0
        inventory_is_plate = inventory == DynamicObject.PLATE
        inventory_is_dish = (inventory & DynamicObject.COOKED) != 0

        pot_is_cooking = object_is_pot * (interact_extra > 0)
        pot_is_cooked = object_is_pot * (
            interact_ingredients & DynamicObject.COOKED != 0
        )
        pot_is_idle = object_is_pot * ~pot_is_cooking * ~pot_is_cooked

        successful_pickup = (
            object_is_pile * inventory_is_empty
            + pot_is_cooked * inventory_is_plate
            + object_is_wall * ~object_has_no_ingredients * inventory_is_empty
        )

        print("successful_pickup: ", successful_pickup)
        print("object_is_pile: ", object_is_pile)
        print("inventory_is_empty: ", inventory_is_empty)

        pot_full = DynamicObject.ingredient_count(interact_ingredients) == 3

        successful_drop = (
            object_is_wall * object_has_no_ingredients * ~inventory_is_empty
            + pot_is_idle * inventory_is_ingredient * ~pot_full
        )
        successful_delivery = object_is_goal * inventory_is_dish
        no_effect = ~successful_pickup * ~successful_drop * ~successful_delivery

        merged_ingredients = interact_ingredients + inventory
        print("merged_ingredients: ", merged_ingredients)

        pile_ingredient = (
            object_is_plate_pile * DynamicObject.PLATE
            + object_is_ingredient_pile * StaticObject.get_ingredient(interact_item)
        )

        new_ingredients = (
            successful_drop * merged_ingredients + no_effect * interact_ingredients
        )

        new_extra = jax.lax.select(
            pot_is_idle * ~object_has_no_ingredients * inventory_is_empty,
            POT_COOK_TIME,
            interact_extra,
        )
        new_cell = jnp.array([interact_item, new_ingredients, new_extra])

        new_grid = grid.at[fwd_pos.y, fwd_pos.x].set(new_cell)
        new_inventory = (
            successful_pickup * (pile_ingredient + merged_ingredients)
            + no_effect * inventory
        )
        print("new_inventory: ", new_inventory)
        new_agent = agent.replace(inventory=new_inventory)
        reward = jnp.array(successful_delivery, dtype=float) * DELIVERY_REWARD

        return new_grid, new_agent, reward

    def is_terminal(self, state: State) -> bool:
        """Check whether state is terminal."""
        done_steps = state.time >= self.max_steps
        return done_steps | state.terminal

    def get_eval_solved_rate_fn(self):
        def _fn(ep_stats):
            return ep_stats["return"] > 0

        return _fn

    @property
    def name(self) -> str:
        """Environment name."""
        return "Overcooked V2"

    @property
    def num_actions(self) -> int:
        """Number of actions possible in environment."""
        return len(self.action_set)

    def action_space(self, agent_id="") -> spaces.Discrete:
        """Action space of the environment. Agent_id not used since action_space is uniform for all agents"""
        return spaces.Discrete(len(self.action_set), dtype=jnp.uint32)

    def observation_space(self) -> spaces.Box:
        """Observation space of the environment."""
        return spaces.Box(0, 255, self.obs_shape)

    def state_space(self) -> spaces.Dict:
        """State space of the environment."""
        h = self.height
        w = self.width
        agent_view_size = self.agent_view_size
        return spaces.Dict(
            {
                "agent_pos": spaces.Box(0, max(w, h), (2,), dtype=jnp.uint32),
                "agent_dir": spaces.Discrete(4),
                "goal_pos": spaces.Box(0, max(w, h), (2,), dtype=jnp.uint32),
                "maze_map": spaces.Box(
                    0,
                    255,
                    (w + agent_view_size, h + agent_view_size, 3),
                    dtype=jnp.uint32,
                ),
                "time": spaces.Discrete(self.max_steps),
                "terminal": spaces.Discrete(2),
            }
        )

    def max_steps(self) -> int:
        return self.max_steps
