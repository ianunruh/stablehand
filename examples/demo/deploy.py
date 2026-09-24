from pyinfra.operations import server

server.shell(
    name="Echo stablehand demo",
    commands=["echo stablehand-demo"],
)
