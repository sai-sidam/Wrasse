# 🐟 Wrasse: an agent harness that holds the plan

> Your AI is the shark. Wrasse keeps it clean.

**All code in this repo was written on 9/26 (2026) for the hackathon.**

Wrasse is a terminal coding-agent harness. It does real coding work in a sandboxed workspace. Around the agent sits
a layer that holds the plan: gap check, a plan sized to the time you have, detour pricing, return-to-plan, and
rules it learns from your decisions. MongoDB Atlas stores everything.

_Full README (architecture, eval results) lands in build step 6._

## Quick start

```bash
pip install -r requirements-dev.txt && pip install -e .
cp .env.example .env         # set MONGODB_URI and ANTHROPIC_API_KEY
wrasse new demo              # copies workspace_template/ into workspaces/demo/ and opens the chat
wrasse resume demo
python -m pytest -q          # harness tests (mongomock + a scripted fake LLM, no keys needed)
```
