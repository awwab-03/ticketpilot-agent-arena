# Submission summary — Agentic AI Assignment 1 (Agent Arena)

- **Full name:** TODO
- **Roll number:** TODO (lowercase, e.g. i221234)
- **Class / section:** TODO
- **University email:** TODO
- **GitHub username:** awwab-03
- **Agent name:** TicketPilot
- **Domain:** Support-Ticket Triage (domain menu #11)

- **GitHub repository URL:** TODO https://github.com/awwab-03/REPOSITORY
- **Final commit hash:** TODO (output of `git rev-parse HEAD`)
- **Working agent interface:** TODO https://YOUR-APP.onrender.com/
- **Health endpoint (GET):** TODO https://YOUR-APP.onrender.com/health
- **Arena endpoint (POST):** TODO https://YOUR-APP.onrender.com/arena/run
- **Manifest endpoint (GET):** TODO https://YOUR-APP.onrender.com/arena/manifest
- **API documentation:** TODO https://YOUR-APP.onrender.com/docs

- **Hosting provider:** Render (free web service, 1 worker)
- **Default model / provider:** gemini-3.1-flash-lite / Google Gemini API
- **Other available models:** gemini-3.5-flash-lite, gemini-3.6-flash (UI selector)
- **Example input:** `{"task": "Triage ticket T-1001.", "external_context": [], "arena_config": {"max_steps": 6, "fault": "none"}}`
- **Expected result:** `completed / goal_completed` in 2 steps. `get_ticket` then `triage_ticket`, with T-1001 set to billing, P2, queue billing.
- **Cold-start / restart limitations:** The Render free instance sleeps after about 15 minutes idle, so the first request can take about 30–60 s. Chat history is in memory (one worker) and is lost on restart or redeploy. Arena runs are stateless, and each run uses a fresh sandbox copy of the sample data.
- **Repository access:** TODO instructor invited (pending / accepted)
- **Public test results:** `evaluation/results/public_results_local.md` (local, 22/22) and `evaluation/results/public_results_deployed.md` (deployed); model comparison in `evaluation/results/model_comparison.md`

Submit `<roll>.zip` (one top-level `<roll>/` folder containing this file) and `<roll>_submission.pdf` in Google Classroom, then click **Turn in**.
