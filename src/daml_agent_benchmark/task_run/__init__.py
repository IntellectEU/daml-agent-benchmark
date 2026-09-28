"""Running one task, and each attempt the agent makes at it.

`task` drives a task from its repository copy to its record. `repo_copy`,
`ground_truth` and `attempt` are the three steps it takes, and the modules around
them are what an attempt needs: the command and environment, the prompt and the
agent's home, the driver that speaks JSON-RPC to the agent, the events it reports,
and how its outcome is classified.
"""
