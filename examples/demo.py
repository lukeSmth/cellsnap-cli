# ---
# jupyter:
#   jupytext:
#     cell_metadata_filter: -all
#     text_representation:
#       extension: .py
#       format_name: percent
#       format_version: '1.3'
#       jupytext_version: 1.19.4
#   kernelspec:
#     display_name: .venv
#     language: python
#     name: python3
# ---

# %% [markdown]
# # `cellsnap-cli` demo
#
# The `.py` file is the git tracked half of the jupytext pair. The `.ipynb` is published to a cellsnap artifact branch.
#
# **Staying in sync:**
#
# `cellsnap-cli` compares this script's cells against the notebook's before publishing. Edit one
# without re-syncing the other and the artifact is reported as `drift:` and carried forward
# unchanged rather than being published.

# %%
import matplotlib.pyplot as plt
import numpy as np

# %%
# Data for plotting
t = np.arange(0.0, 2.0, 0.01)
s = 1 + np.sin(2 * np.pi * t)

# %%
fig, ax = plt.subplots()
ax.plot(t, s)

ax.set(xlabel="time (s)", ylabel="voltage (mV)")
ax.grid()

plt.show()
