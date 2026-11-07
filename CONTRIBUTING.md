# Contributing

Open an issue describing the problem or send a focused pull request. Include your
OS, Python version, agent versions, and a minimal reproduction with invented data.
Do not attach real transcripts, access tokens, or private source code.

Keep the relay dependency-free. Run `python -m unittest discover -s tests -v`.
Test disconnects and restart behavior when changing transport or persistence.
Test Windows paths containing spaces when changing launch/install scripts.

Changes should explain their effect on the local trust boundary and preserve
existing commands unless the migration is documented.
