import json
import os
import queue
import re
import shutil
import subprocess
import threading
import time
import tkinter as tk
from dataclasses import dataclass
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk


SYSTEM_PROMPT = """
Tu es un ingénieur logiciel local. Tu dois produire un plan puis des actions JSON.
Format attendu strictement:
{
  "summary": "...",
  "actions": [
    {"type": "mkdir", "path": "..."},
    {"type": "write_file", "path": "...", "content": "..."},
    {"type": "append_file", "path": "...", "content": "..."},
    {"type": "read_file", "path": "..."},
    {"type": "run", "cmd": "..."},
    {"type": "done", "reason": "..."}
  ]
}
Contraintes:
- Toutes les actions sont locales.
- Ne jamais utiliser d'API distante.
- Privilégier CMake pour C++ et pytest/unittest pour Python.
- Corriger les erreurs de build/test dans les itérations suivantes.
""".strip()


@dataclass
class AgentConfig:
    model: str = "llama3.1"
    project_dir: str = ""
    description: str = ""
    language: str = "python"
    max_iterations: int = 50
    target_loc: int = 250000


class LocalLLMClient:
    """Client minimal pour appeler un LLM local via la CLI ollama."""

    def __init__(self, model: str, timeout: int = 120):
        self.model = model
        self.timeout = timeout

    def generate(self, prompt: str) -> str:
        cmd = ["ollama", "run", self.model, prompt]
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                check=False,
            )
        except FileNotFoundError as exc:
            raise RuntimeError("Ollama n'est pas installé ou introuvable dans le PATH.") from exc
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("Temps d'exécution du modèle local dépassé.") from exc

        if result.returncode != 0:
            stderr = result.stderr.strip() or "Erreur inconnue"
            raise RuntimeError(f"Échec appel LLM local: {stderr}")
        return result.stdout.strip()


class WorkspaceExecutor:
    def __init__(self, root: Path, logger):
        self.root = root
        self.logger = logger

    def _resolve(self, rel_path: str) -> Path:
        safe = (self.root / rel_path).resolve()
        if not str(safe).startswith(str(self.root.resolve())):
            raise ValueError(f"Chemin hors workspace interdit: {rel_path}")
        return safe

    def mkdir(self, rel_path: str):
        path = self._resolve(rel_path)
        path.mkdir(parents=True, exist_ok=True)
        self.logger(f"[mkdir] {path}")

    def write_file(self, rel_path: str, content: str):
        path = self._resolve(rel_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        self.logger(f"[write_file] {path} ({len(content)} chars)")

    def append_file(self, rel_path: str, content: str):
        path = self._resolve(rel_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(content)
        self.logger(f"[append_file] {path} (+{len(content)} chars)")

    def read_file(self, rel_path: str) -> str:
        path = self._resolve(rel_path)
        if not path.exists():
            self.logger(f"[read_file] introuvable: {path}")
            return ""
        content = path.read_text(encoding="utf-8", errors="replace")
        self.logger(f"[read_file] {path} ({len(content)} chars)")
        return content

    def run(self, command: str) -> str:
        self.logger(f"[run] {command}")
        result = subprocess.run(
            command,
            shell=True,
            cwd=self.root,
            capture_output=True,
            text=True,
            check=False,
        )
        output = f"$ {command}\n{result.stdout}\n{result.stderr}".strip()
        self.logger(f"[run:exit={result.returncode}]\n{output[:2000]}")
        return output

    def count_loc(self, language: str) -> int:
        ext_map = {"python": ".py", "cpp": (".cpp", ".hpp", ".h", ".cc", ".cxx")}
        extensions = ext_map.get(language.lower(), ".py")
        total = 0
        for p in self.root.rglob("*"):
            if p.is_file():
                if isinstance(extensions, tuple):
                    match = p.suffix.lower() in extensions
                else:
                    match = p.suffix.lower() == extensions
                if match:
                    try:
                        total += len(p.read_text(encoding="utf-8", errors="ignore").splitlines())
                    except Exception:
                        continue
        return total


class AutoDevAgent:
    def __init__(self, config: AgentConfig, llm: LocalLLMClient, executor: WorkspaceExecutor, logger):
        self.config = config
        self.llm = llm
        self.executor = executor
        self.logger = logger
        self.stop_requested = False

    def stop(self):
        self.stop_requested = True

    def _extract_json(self, raw: str) -> dict:
        match = re.search(r"\{.*\}", raw, re.S)
        if not match:
            raise ValueError("Réponse LLM sans JSON exploitable.")
        return json.loads(match.group(0))

    def _build_prompt(self, iteration: int, history: str, last_output: str) -> str:
        return f"""
{SYSTEM_PROMPT}

Iteration: {iteration}/{self.config.max_iterations}
Langage cible: {self.config.language}
Objectif LOC approximatif: {self.config.target_loc}
Description utilisateur:
{self.config.description}

Historique résumé:
{history[-5000:]}

Dernière sortie build/test:
{last_output[-4000:]}

Renvoie UNIQUEMENT le JSON.
""".strip()

    def run_cycle(self):
        history = ""
        last_output = ""
        for i in range(1, self.config.max_iterations + 1):
            if self.stop_requested:
                self.logger("Arrêt demandé par l'utilisateur.")
                break

            loc = self.executor.count_loc(self.config.language)
            self.logger(f"\n=== Itération {i} | LOC actuel: {loc} ===")

            if loc >= self.config.target_loc:
                self.logger("Objectif de volume atteint. Passage en finalisation.")

            prompt = self._build_prompt(i, history, last_output)
            raw = self.llm.generate(prompt)
            self.logger(f"[llm] réponse brute (extrait):\n{raw[:1200]}")

            try:
                plan = self._extract_json(raw)
            except Exception as exc:
                self.logger(f"Échec parsing JSON: {exc}")
                last_output = str(exc)
                continue

            summary = plan.get("summary", "(sans résumé)")
            actions = plan.get("actions", [])
            self.logger(f"[plan] {summary} | actions={len(actions)}")
            history += f"\nIteration {i}: {summary}"

            iteration_output = []
            for action in actions:
                if self.stop_requested:
                    break
                a_type = action.get("type", "")
                try:
                    if a_type == "mkdir":
                        self.executor.mkdir(action["path"])
                    elif a_type == "write_file":
                        self.executor.write_file(action["path"], action.get("content", ""))
                    elif a_type == "append_file":
                        self.executor.append_file(action["path"], action.get("content", ""))
                    elif a_type == "read_file":
                        content = self.executor.read_file(action["path"])
                        iteration_output.append(f"READ<{action['path']}>\n{content[:5000]}")
                    elif a_type == "run":
                        out = self.executor.run(action["cmd"])
                        iteration_output.append(out)
                    elif a_type == "done":
                        reason = action.get("reason", "Terminé.")
                        self.logger(f"[done] {reason}")
                        return
                    else:
                        self.logger(f"Action inconnue ignorée: {a_type}")
                except Exception as exc:
                    err = f"Action {a_type} échouée: {exc}"
                    self.logger(err)
                    iteration_output.append(err)

            last_output = "\n\n".join(iteration_output)


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Local AI Developer Studio")
        self.geometry("980x700")
        self.queue = queue.Queue()
        self.agent = None
        self.worker = None

        self._build_ui()
        self.after(200, self._drain_logs)

    def _build_ui(self):
        frm = ttk.Frame(self)
        frm.pack(fill="both", expand=True, padx=10, pady=10)

        top = ttk.LabelFrame(frm, text="Configuration")
        top.pack(fill="x", pady=6)

        self.project_var = tk.StringVar(value=str(Path.cwd() / "generated_project"))
        self.model_var = tk.StringVar(value="llama3.1")
        self.lang_var = tk.StringVar(value="python")
        self.iter_var = tk.StringVar(value="40")
        self.loc_var = tk.StringVar(value="250000")

        ttk.Label(top, text="Dossier projet:").grid(row=0, column=0, sticky="w", padx=4, pady=4)
        ttk.Entry(top, textvariable=self.project_var, width=70).grid(row=0, column=1, sticky="ew", padx=4, pady=4)
        ttk.Button(top, text="Parcourir", command=self._pick_project).grid(row=0, column=2, padx=4, pady=4)

        ttk.Label(top, text="Modèle local (ollama):").grid(row=1, column=0, sticky="w", padx=4, pady=4)
        ttk.Entry(top, textvariable=self.model_var, width=25).grid(row=1, column=1, sticky="w", padx=4, pady=4)

        ttk.Label(top, text="Langage:").grid(row=1, column=1, sticky="e", padx=4, pady=4)
        ttk.Combobox(top, textvariable=self.lang_var, values=["python", "cpp"], width=10, state="readonly").grid(row=1, column=2, sticky="w", padx=4, pady=4)

        ttk.Label(top, text="Itérations max:").grid(row=2, column=0, sticky="w", padx=4, pady=4)
        ttk.Entry(top, textvariable=self.iter_var, width=12).grid(row=2, column=1, sticky="w", padx=4, pady=4)

        ttk.Label(top, text="Objectif LOC:").grid(row=2, column=1, sticky="e", padx=4, pady=4)
        ttk.Entry(top, textvariable=self.loc_var, width=12).grid(row=2, column=2, sticky="w", padx=4, pady=4)

        top.columnconfigure(1, weight=1)

        desc_box = ttk.LabelFrame(frm, text="Description du programme à générer")
        desc_box.pack(fill="both", expand=False, pady=6)
        self.desc_txt = scrolledtext.ScrolledText(desc_box, height=8, wrap="word")
        self.desc_txt.pack(fill="both", expand=True, padx=4, pady=4)
        self.desc_txt.insert("1.0", "Créer une application complète avec architecture modulaire, tests, CI locale et documentation.")

        btns = ttk.Frame(frm)
        btns.pack(fill="x", pady=6)
        ttk.Button(btns, text="Démarrer cycle IA", command=self.start_agent).pack(side="left", padx=4)
        ttk.Button(btns, text="Arrêter", command=self.stop_agent).pack(side="left", padx=4)
        ttk.Button(btns, text="Nettoyer projet", command=self.clean_project).pack(side="left", padx=4)

        logs = ttk.LabelFrame(frm, text="Logs")
        logs.pack(fill="both", expand=True, pady=6)
        self.log_txt = scrolledtext.ScrolledText(logs, wrap="word")
        self.log_txt.pack(fill="both", expand=True, padx=4, pady=4)

    def _pick_project(self):
        selected = filedialog.askdirectory()
        if selected:
            self.project_var.set(selected)

    def _log(self, msg: str):
        self.queue.put(msg)

    def _drain_logs(self):
        while not self.queue.empty():
            msg = self.queue.get_nowait()
            ts = time.strftime("%H:%M:%S")
            self.log_txt.insert("end", f"[{ts}] {msg}\n")
            self.log_txt.see("end")
        self.after(200, self._drain_logs)

    def _run_agent_thread(self, cfg: AgentConfig):
        try:
            root = Path(cfg.project_dir)
            root.mkdir(parents=True, exist_ok=True)
            llm = LocalLLMClient(cfg.model)
            executor = WorkspaceExecutor(root=root, logger=self._log)
            self.agent = AutoDevAgent(cfg, llm=llm, executor=executor, logger=self._log)
            self.agent.run_cycle()
            self._log("Cycle IA terminé.")
        except Exception as exc:
            self._log(f"Erreur fatale: {exc}")

    def start_agent(self):
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("Info", "Un cycle est déjà en cours.")
            return

        description = self.desc_txt.get("1.0", "end").strip()
        if not description:
            messagebox.showwarning("Validation", "La description est requise.")
            return

        try:
            cfg = AgentConfig(
                model=self.model_var.get().strip(),
                project_dir=self.project_var.get().strip(),
                description=description,
                language=self.lang_var.get().strip(),
                max_iterations=int(self.iter_var.get().strip()),
                target_loc=int(self.loc_var.get().strip()),
            )
        except ValueError:
            messagebox.showerror("Erreur", "Itérations et LOC doivent être des entiers.")
            return

        self.worker = threading.Thread(target=self._run_agent_thread, args=(cfg,), daemon=True)
        self.worker.start()
        self._log("Cycle IA démarré.")

    def stop_agent(self):
        if self.agent:
            self.agent.stop()
            self._log("Demande d'arrêt envoyée.")

    def clean_project(self):
        project = Path(self.project_var.get().strip())
        if project.exists() and project.is_dir():
            if messagebox.askyesno("Confirmer", f"Supprimer le dossier {project} ?"):
                shutil.rmtree(project)
                self._log(f"Projet supprimé: {project}")


if __name__ == "__main__":
    app = App()
    app.mainloop()
