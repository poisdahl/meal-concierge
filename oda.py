"""Compatibility entrypoint for the shipped Oda/Mathem preflight command.

Shared transport and helpers live in retail_mcp; provider identities and the
existing command-line arguments are unchanged.
"""

from retail_mcp import main


if __name__ == "__main__":
    main()
