import jax
import jax.numpy as jnp
from typing import List
import itertools
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

def mark_adjacent_cells(mask):
    # Shift the mask in four directions: up, down, left, right
    up = jnp.roll(mask, shift=-1, axis=0)
    down = jnp.roll(mask, shift=1, axis=0)
    left = jnp.roll(mask, shift=-1, axis=1)
    right = jnp.roll(mask, shift=1, axis=1)
    
    # Prevent wrapping by zeroing out the rolled values at the boundaries
    up = up.at[-1, :].set(False)
    down = down.at[0, :].set(False)
    left = left.at[:, -1].set(False)
    right = right.at[:, 0].set(False)

    # Combine the original mask with the shifted versions
    expanded_mask = mask | up | down | left | right
    
    return expanded_mask


def get_closest_true_pos(arr: jnp.ndarray, pos: Position) -> Position:
    height, width = arr.shape

    y, x = pos.y, pos.x
    yy, xx = jnp.meshgrid(jnp.arange(height), jnp.arange(width), indexing="ij")

    dist = jnp.abs(yy - y) + jnp.abs(xx - x)
    dist = jnp.where(arr, dist, jnp.inf)

    min_idx = jnp.argmin(dist)
    min_y, min_x = jnp.divmod(min_idx, width)

    is_valid = jnp.any(arr)

    return Position(x=min_x, y=min_y), is_valid
