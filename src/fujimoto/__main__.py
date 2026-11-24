"""`python -m fujimoto`: the same entry point as the `fujimoto` script.

The MCP server runs its mutating tools this way (`sys.executable -m
fujimoto …`), so they use the fujimoto install the server itself runs from
rather than whichever `fujimoto` is first on a GUI app's PATH.
"""

from fujimoto.cli import main

if __name__ == "__main__":
    main()
