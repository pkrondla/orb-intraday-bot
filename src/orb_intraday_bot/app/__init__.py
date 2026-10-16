"""Application services: what each command does, without printing or parsing arguments.

`cli.py` turns arguments into a request, calls a service here, and prints the answer. A service
takes its collaborators (market data, store, clock) as parameters, so tests drive it with fakes
and a future scheduler or API can call the same code.
"""
