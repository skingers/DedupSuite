# Environment Setup and Developer Guide

Follow this guide to isolate your local environment, install dependencies, run the platform orchestrator, and execute the automated integrity validators.

---

## 1. Virtual Environment Isolation

To prevent package drift, dependency conflicts, or MCP runtime collisions, sovraan must be run inside an isolated Python virtual environment (`.venv`).

### Step 1: Initialize Virtual Environment
Navigate to the root directory of the project and create the environment:
```powershell
# Windows PowerShell
python -m venv .venv
```

### Step 2: Activate the Environment
Always ensure the active terminal session runs inside the isolated context:
```powershell
# PowerShell
.venv\Scripts\Activate.ps1

# Windows Command Prompt (Cmd)
.venv\Scripts\activate.bat
```

### Step 3: Application configuration
Copy the settings template before first launch (keeps personal paths out of git):

```powershell
Copy-Item config.json.example config.json
```

### Step 4: Install Core Dependencies
Install the required packages listed in the manifest:
```bash
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

---

## 2. Launching the Platform Orchestrator


The system orchestration GUI executes locally. Ensure your virtual environment is active before starting the program:

```bash
python sovraan_core.py
```

*Note: The GUI will automatically create or migration-check `data_mine.db` and scan configurations upon startup.*

---

## 3. Running Automated Integrity Tests

Automated testing is configured using `pytest`. All tests must pass successfully before committing changes to GitLab.

### Run All Test Suites
Execute all automated validators:
```bash
python -m pytest
```

### Run Knowledge Graph Exporter Audit
Run the structure audit on manifest files:
```bash
python -m pytest tests/test_knowledge_graph_exporter.py
```

### Run Vector Engine & Persistence Audit
Validate that ChromaDB indexing and persistence queries are functioning:
```bash
python -m pytest tests/test_vector_engine.py
```

### Run the Ultimate Integrity Gate (Fidelity Validator)
Execute the 10-point semantic query and binary verification loop:
```bash
python -m pytest tests/test_validator.py
```

---

## 4. Obsidian Plugin Integration

The production Obsidian bridge lives in `obsidian-plugin/` inside this repository. A separate **free** community distribution is maintained at `sovraan-obsidian-plugin` (GitLab).

### Embedded plugin (this repo)

| Setting | Value |
|---|---|
| Plugin ID | `sovraan-obsidian-satellite` |
| Backend script | `sovraan_core.py` |
| Headless contract | `--headless --source <vault> --destination <vault> --db <data_mine.db> --notarise` |

Build the plugin from `obsidian-plugin/`:

```bash
cd obsidian-plugin
npm install
npm run build
```

Copy `dist/` into your vault at `.obsidian/plugins/sovraan-obsidian-satellite/`.

### Pro license (freemium cap)

Unlicensed audits enforce a **2000 processed-file cap** with HMAC-signed local state (`sovraan_processed_state.json`). Pass a valid Sovraan Pro JWT via:

```bash
python sovraan_core.py --headless --source C:\path\to\files --db C:\path\to\data_mine.db --license "<JWT>"
```

The Obsidian plugin forwards `--license` from its `proLicenseKey` setting using the same contract.

### Community plugin (separate repo)

The community plugin does not bundle the commercial Python engine. Users configure local paths to their installed sovraan build. Keep CLI arguments aligned with `obsidian-plugin/src/execution_engine.ts` when changing headless mode.

---

## 5. GitLab Developer Workflow

All repositories and build tasks are hosted exclusively on GitLab. 

### Development Rules
1.  **Isolated Changes**: Write feature branches targeting specific issues. Do not commit directly to the `main` branch.
2.  **Pre-Commit Verification**: Run `python -m pytest` locally before initiating merge requests.
3.  **Pipeline Monitoring**: Ensure that the GitLab CI/CD runner pipeline passes.
4.  **No Public/GitHub Pushes**: Do not push code or mirrors to GitHub or other public hosting services; all proprietary code must remain in the secure GitLab registry.
