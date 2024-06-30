import jax
import jax.numpy as jnp
from typing import List
import itertools
import chex
from collections import deque
from .common import Position, Direction


def tree_select(predicate, a, b):
    return jax.tree_util.tree_map(lambda x, y: jax.lax.select(predicate, x, y), a, b)


def compute_view_box(x, y, agent_view_size, height, width):
    """Compute the view box for an agent centered at (x, y)"""
    x_low = x - agent_view_size
    x_high = x + agent_view_size + 1
    y_low = y - agent_view_size
    y_high = y + agent_view_size + 1

    x_low = jax.lax.clamp(0, x_low, width)
    x_high = jax.lax.clamp(0, x_high, width)
    y_low = jax.lax.clamp(0, y_low, height)
    y_high = jax.lax.clamp(0, y_high, height)

    return x_low, x_high, y_low, y_high


def get_possible_recipes(num_ingredients: int) -> List[List[int]]:
    """
    Get all possible recipes given the number of ingredients.
    """
    available_ingredients = list(range(num_ingredients)) * 3
    raw_combinations = itertools.combinations(available_ingredients, 3)
    unique_recipes = set(tuple(sorted(combination)) for combination in raw_combinations)
    possible_recipes = jnp.array(list(unique_recipes), dtype=jnp.int32)

    return possible_recipes


def compute_enclosed_spaces(empty_mask: jnp.ndarray) -> jnp.ndarray:
    """
    Compute the enclosed spaces in the environment.
    Each enclosed space is assigned a unique id.
    """
    height, width = empty_mask.shape
    id_grid = jnp.arange(empty_mask.size, dtype=jnp.int32).reshape(empty_mask.shape)
    id_grid = jnp.where(empty_mask, id_grid, -1)

    def _body_fun(val):
        _, curr = val

        def _next_val(pos):
            neighbors = jax.vmap(pos.move_in_bounds, in_axes=(0, None, None))(
                jnp.array(list(Direction)), width, height
            )
            neighbour_values = curr[neighbors.y, neighbors.x]
            self_value = curr[pos.y, pos.x]
            values = jnp.concatenate(
                [neighbour_values, self_value[jnp.newaxis]], axis=0
            )
            new_val = jnp.max(values)
            return jax.lax.select(self_value == -1, self_value, new_val)

        pos_y, pos_x = jnp.meshgrid(
            jnp.arange(height), jnp.arange(width), indexing="ij"
        )

        next_vals = jax.vmap(jax.vmap(_next_val))(Position(x=pos_x, y=pos_y))
        stop = jnp.all(curr == next_vals)
        return stop, next_vals

    def _cond_fun(val):
        return ~val[0]

    initial_val = (False, id_grid)
    _, res = jax.lax.while_loop(_cond_fun, _body_fun, initial_val)
    return res
