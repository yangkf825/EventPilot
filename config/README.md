# Configuration

`online_v2_authoring.py` defines the fixed 20 author profiles, real source IDs,
research adaptations, rules, retained constraints and checkpoint audit policy.
It never contains a fabricated completed trajectory.

Copy `../models.example.json` to the repository root as `models.gateway.json`.
Edit each model's complete chat-completions URL and exact provider model ID.
Keys belong only in the terminal environment variables named by `api_key_env`.
The runner does not load `.env` automatically. See the root README.
