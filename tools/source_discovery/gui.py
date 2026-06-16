from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

import discover


SCRIPT_DIR = Path(__file__).resolve().parent
OUTPUT_PATH = SCRIPT_DIR / "candidate_sources.discovered.yml"


class DiscoveryGui(tk.Tk):
    def __init__(self):
        super().__init__()

        self.title("Trans Resource Source Discovery")
        self.geometry("900x620")
        self.minsize(760, 500)

        self.log_queue: queue.Queue[str] = queue.Queue()
        self.worker_thread: threading.Thread | None = None

        self.min_score = tk.IntVar(value=45)
        self.timeout_seconds = tk.IntVar(value=60)
        self.per_query_limit = tk.IntVar(value=80)
        self.max_indexes = tk.IntVar(value=4)
        self.expand_sitemaps = tk.BooleanVar(value=True)

        self._build_ui()
        self.after(100, self._drain_log_queue)

    def _build_ui(self):
        outer = ttk.Frame(self, padding=12)
        outer.pack(fill="both", expand=True)

        title = ttk.Label(
            outer,
            text="Trans Resource Source Discovery",
            font=("Segoe UI", 16, "bold"),
        )
        title.pack(anchor="w")

        subtitle = ttk.Label(
            outer,
            text=(
                "Finds candidate trusted sources using Common Crawl, excludes existing sources.yml, "
                "and writes a separate candidate_sources.discovered.yml file."
            ),
            wraplength=850,
        )
        subtitle.pack(anchor="w", pady=(4, 12))

        controls = ttk.LabelFrame(outer, text="Discovery settings", padding=10)
        controls.pack(fill="x", pady=(0, 10))

        row1 = ttk.Frame(controls)
        row1.pack(fill="x", pady=4)

        ttk.Label(row1, text="Minimum score").pack(side="left")
        ttk.Entry(row1, textvariable=self.min_score, width=8).pack(side="left", padx=(6, 18))

        ttk.Label(row1, text="Timeout seconds").pack(side="left")
        ttk.Entry(row1, textvariable=self.timeout_seconds, width=8).pack(side="left", padx=(6, 18))

        ttk.Label(row1, text="Per-query limit").pack(side="left")
        ttk.Entry(row1, textvariable=self.per_query_limit, width=8).pack(side="left", padx=(6, 18))

        ttk.Label(row1, text="Common Crawl indexes").pack(side="left")
        ttk.Entry(row1, textvariable=self.max_indexes, width=8).pack(side="left", padx=(6, 18))

        row2 = ttk.Frame(controls)
        row2.pack(fill="x", pady=4)

        ttk.Checkbutton(
            row2,
            text="Expand promising domains with sitemap discovery",
            variable=self.expand_sitemaps,
        ).pack(side="left")

        buttons = ttk.Frame(outer)
        buttons.pack(fill="x", pady=(0, 10))

        self.run_button = ttk.Button(
            buttons,
            text="Run discovery",
            command=self.run_discovery,
        )
        self.run_button.pack(side="left")

        ttk.Button(
            buttons,
            text="Open output YAML",
            command=self.open_output_yaml,
        ).pack(side="left", padx=8)

        ttk.Button(
            buttons,
            text="Open output folder",
            command=self.open_output_folder,
        ).pack(side="left")

        ttk.Button(
            buttons,
            text="Clear log",
            command=self.clear_log,
        ).pack(side="right")

        log_frame = ttk.LabelFrame(outer, text="Log", padding=8)
        log_frame.pack(fill="both", expand=True)

        self.log_text = tk.Text(log_frame, wrap="word", height=24)
        self.log_text.pack(side="left", fill="both", expand=True)

        scrollbar = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        scrollbar.pack(side="right", fill="y")
        self.log_text.configure(yscrollcommand=scrollbar.set)

        self.write_log("Ready.")
        self.write_log(f"Output will be written to: {OUTPUT_PATH}")

    def write_log(self, message: str):
        self.log_queue.put(message)

    def _drain_log_queue(self):
        while True:
            try:
                message = self.log_queue.get_nowait()
            except queue.Empty:
                break

            self.log_text.insert("end", message + "\n")
            self.log_text.see("end")

        self.after(100, self._drain_log_queue)

    def clear_log(self):
        self.log_text.delete("1.0", "end")

    def run_discovery(self):
        if self.worker_thread and self.worker_thread.is_alive():
            messagebox.showinfo("Already running", "Discovery is already running.")
            return

        try:
            min_score = int(self.min_score.get())
            timeout_seconds = int(self.timeout_seconds.get())
            per_query_limit = int(self.per_query_limit.get())
            max_indexes = int(self.max_indexes.get())
        except Exception:
            messagebox.showerror("Invalid settings", "Please use whole numbers for numeric settings.")
            return

        if timeout_seconds < 10:
            messagebox.showerror("Timeout too low", "Use at least 10 seconds. 60 is recommended.")
            return

        if min_score < 0:
            messagebox.showerror("Invalid score", "Minimum score must be 0 or higher.")
            return

        self.run_button.configure(state="disabled")
        self.write_log("")
        self.write_log("Starting discovery...")

        self.worker_thread = threading.Thread(
            target=self._worker,
            kwargs={
                "min_score": min_score,
                "timeout_seconds": timeout_seconds,
                "per_query_limit": per_query_limit,
                "max_indexes": max_indexes,
                "expand_sitemaps": bool(self.expand_sitemaps.get()),
            },
            daemon=True,
        )
        self.worker_thread.start()

    def _worker(
        self,
        min_score: int,
        timeout_seconds: int,
        per_query_limit: int,
        max_indexes: int,
        expand_sitemaps: bool,
    ):
        try:
            output = discover.run(
                min_score=min_score,
                timeout_seconds=timeout_seconds,
                per_query_limit=per_query_limit,
                max_indexes=max_indexes,
                expand_sitemaps=expand_sitemaps,
                log=self.write_log,
            )
            self.write_log(f"Finished successfully: {output}")
        except Exception as error:
            self.write_log(f"ERROR: {error}")
            self.after(0, lambda: messagebox.showerror("Discovery failed", str(error)))
        finally:
            self.after(0, lambda: self.run_button.configure(state="normal"))

    def open_output_yaml(self):
        if not OUTPUT_PATH.exists():
            messagebox.showinfo(
                "No output yet",
                "Run discovery first. The YAML file does not exist yet.",
            )
            return

        self._open_path(OUTPUT_PATH)

    def open_output_folder(self):
        self._open_path(SCRIPT_DIR)

    def _open_path(self, path: Path):
        path = path.resolve()

        try:
            if sys.platform.startswith("win"):
                os.startfile(path)  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.run(["open", str(path)], check=False)
            else:
                subprocess.run(["xdg-open", str(path)], check=False)
        except Exception as error:
            messagebox.showerror("Could not open path", str(error))


if __name__ == "__main__":
    app = DiscoveryGui()
    app.mainloop()
