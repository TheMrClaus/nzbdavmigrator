#!/usr/bin/env python3
"""
NZBDAVMigrator GUI Application
A graphical interface for managing NZB migrations with Radarr/Sonarr integration.
"""

import tkinter as tk
from tkinter import ttk, messagebox, filedialog
import os
import sys
import json
import threading
from pathlib import Path
from typing import Dict, List, Optional, Tuple
import sqlite3
from datetime import datetime

# Import from the existing CLI tool
try:
    from export_nzb import (
        parse_release_dir, is_series, is_movie,
        extract_series_title, extract_movie_title,
        trigger_radarr_searches, trigger_sonarr_searches,
        _api_request
    )
except ImportError:
    print("Error: Cannot import from export_nzb.py. Make sure it's in the same directory.")
    sys.exit(1)


class Config:
    """Configuration management for the GUI application"""

    def __init__(self):
        self.config_file = Path("nzbdav_gui_config.json")
        self.defaults = {
            "database_path": "db.sqlite",
            "radarr_url": "",
            "radarr_api_key": "",
            "sonarr_url": "",
            "sonarr_api_key": "",
            "batch_size": 10,
            "max_batch_size": 50,
            "api_delay": 2.0,
            "window_geometry": "1200x800",
            "status_db": "nzbdav_status.db"
        }
        self.data = self.load()

    def load(self) -> Dict:
        """Load configuration from file"""
        if self.config_file.exists():
            try:
                with open(self.config_file, 'r') as f:
                    config = json.load(f)
                # Merge with defaults to handle new settings
                merged = self.defaults.copy()
                merged.update(config)
                return merged
            except (json.JSONDecodeError, IOError) as e:
                print(f"Warning: Failed to load config: {e}")
        return self.defaults.copy()

    def save(self):
        """Save configuration to file"""
        try:
            with open(self.config_file, 'w') as f:
                json.dump(self.data, f, indent=2)
        except IOError as e:
            print(f"Warning: Failed to save config: {e}")

    def get(self, key: str, default=None):
        """Get configuration value"""
        return self.data.get(key, default)

    def set(self, key: str, value):
        """Set configuration value"""
        self.data[key] = value


class StatusDatabase:
    """Database for tracking processing status"""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._init_db()

    def _init_db(self):
        """Initialize status tracking database"""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS processed_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    category TEXT NOT NULL,
                    release_path TEXT NOT NULL,
                    media_type TEXT NOT NULL,  -- 'movie' or 'series'
                    processed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    status TEXT DEFAULT 'processed',  -- 'processed', 'failed', 'skipped'
                    error_message TEXT,
                    UNIQUE(title, category, release_path)
                )
            """)
            conn.commit()

    def add_processed(self, title: str, category: str, release_path: str,
                     media_type: str, status: str = 'processed', error: str = None):
        """Add a processed item"""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                INSERT OR REPLACE INTO processed_items
                (title, category, release_path, media_type, status, error_message)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (title, category, release_path, media_type, status, error))
            conn.commit()

    def is_processed(self, title: str, category: str, release_path: str) -> bool:
        """Check if an item has been processed"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute("""
                SELECT 1 FROM processed_items
                WHERE title = ? AND category = ? AND release_path = ?
            """, (title, category, release_path))
            return cursor.fetchone() is not None

    def get_processed_items(self) -> List[Tuple]:
        """Get all processed items"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute("""
                SELECT title, category, release_path, media_type, processed_at, status, error_message
                FROM processed_items ORDER BY processed_at DESC
            """)
            return cursor.fetchall()

    def clear_processed(self, title: str = None, category: str = None, release_path: str = None):
        """Clear processed status for specific items or all items"""
        with sqlite3.connect(self.db_path) as conn:
            if title and category and release_path:
                conn.execute("""
                    DELETE FROM processed_items
                    WHERE title = ? AND category = ? AND release_path = ?
                """, (title, category, release_path))
            else:
                conn.execute("DELETE FROM processed_items")
            conn.commit()


class NZBDAVMigratorGUI:
    """Main GUI application class"""

    def __init__(self):
        self.config = Config()
        self.status_db = StatusDatabase(self.config.get("status_db"))
        self.root = tk.Tk()
        self.items_data = []  # Store all loaded items
        self.filtered_items = []  # Store filtered items for display
        self.processing_thread = None
        self.processing_cancelled = False

        self.setup_gui()
        self.load_items()

    def setup_gui(self):
        """Setup the main GUI interface"""
        self.root.title("NZBDAVMigrator - Media Management Tool")
        self.root.geometry(self.config.get("window_geometry"))

        # Configure grid weights
        self.root.grid_rowconfigure(1, weight=1)
        self.root.grid_columnconfigure(0, weight=1)

        self.create_menu()
        self.create_toolbar()
        self.create_main_content()
        self.create_status_bar()

        # Bind window close event
        self.root.protocol("WM_DELETE_WINDOW", self.on_closing)

    def create_menu(self):
        """Create the application menu"""
        menubar = tk.Menu(self.root)
        self.root.config(menu=menubar)

        # File menu
        file_menu = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="File", menu=file_menu)
        file_menu.add_command(label="Change Database...", command=self.change_database)
        file_menu.add_separator()
        file_menu.add_command(label="Exit", command=self.on_closing)

        # Settings menu
        settings_menu = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="Settings", menu=settings_menu)
        settings_menu.add_command(label="Configure Radarr/Sonarr...", command=self.open_settings)

        # Help menu
        help_menu = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="Help", menu=help_menu)
        help_menu.add_command(label="About", command=self.show_about)

    def create_toolbar(self):
        """Create the toolbar with action buttons"""
        toolbar = ttk.Frame(self.root)
        toolbar.grid(row=0, column=0, sticky="ew", padx=5, pady=5)

        # Refresh button
        ttk.Button(toolbar, text="Refresh", command=self.refresh_items).pack(side=tk.LEFT, padx=2)

        # Selection buttons
        ttk.Separator(toolbar, orient=tk.VERTICAL).pack(side=tk.LEFT, padx=10, fill=tk.Y)
        ttk.Button(toolbar, text="Select All", command=self.select_all).pack(side=tk.LEFT, padx=2)
        ttk.Button(toolbar, text="Select None", command=self.select_none).pack(side=tk.LEFT, padx=2)

        # Processing buttons
        ttk.Separator(toolbar, orient=tk.VERTICAL).pack(side=tk.LEFT, padx=10, fill=tk.Y)
        ttk.Button(toolbar, text="Process Selected", command=self.process_selected).pack(side=tk.LEFT, padx=2)
        ttk.Button(toolbar, text="Batch Process All", command=self.batch_process_all).pack(side=tk.LEFT, padx=2)

        # Filter controls
        ttk.Separator(toolbar, orient=tk.VERTICAL).pack(side=tk.LEFT, padx=10, fill=tk.Y)
        ttk.Label(toolbar, text="Filter:").pack(side=tk.LEFT, padx=2)
        self.filter_var = tk.StringVar()
        self.filter_var.trace("w", self.apply_filter)
        filter_entry = ttk.Entry(toolbar, textvariable=self.filter_var, width=20)
        filter_entry.pack(side=tk.LEFT, padx=2)

        # Show processed checkbox
        self.show_processed_var = tk.BooleanVar(value=False)
        self.show_processed_var.trace("w", self.apply_filter)
        ttk.Checkbutton(toolbar, text="Show Processed",
                       variable=self.show_processed_var).pack(side=tk.LEFT, padx=10)

    def create_main_content(self):
        """Create the main content area with notebook tabs"""
        self.notebook = ttk.Notebook(self.root)
        self.notebook.grid(row=1, column=0, sticky="nsew", padx=5, pady=5)

        # Movies tab
        self.movies_frame = ttk.Frame(self.notebook)
        self.notebook.add(self.movies_frame, text="Movies")
        self.create_items_tree(self.movies_frame, "movies")

        # Series tab
        self.series_frame = ttk.Frame(self.notebook)
        self.notebook.add(self.series_frame, text="TV Series")
        self.create_items_tree(self.series_frame, "series")

    def create_items_tree(self, parent: ttk.Frame, tab_type: str):
        """Create the tree view for items"""
        # Configure grid
        parent.grid_rowconfigure(0, weight=1)
        parent.grid_columnconfigure(0, weight=1)

        # Create treeview with scrollbars
        tree_frame = ttk.Frame(parent)
        tree_frame.grid(row=0, column=0, sticky="nsew")
        tree_frame.grid_rowconfigure(0, weight=1)
        tree_frame.grid_columnconfigure(0, weight=1)

        columns = ("Title", "Category", "Release Path", "Media Path", "Status", "Last Processed")
        tree = ttk.Treeview(tree_frame, columns=columns, show="tree headings", selectmode="extended")
        tree.grid(row=0, column=0, sticky="nsew")

        # Configure columns
        tree.column("#0", width=30, minwidth=30)  # Checkbox column
        tree.heading("#0", text="☐")

        for i, col in enumerate(columns):
            tree.column(col, width=150, minwidth=100)
            tree.heading(col, text=col, command=lambda c=col: self.sort_treeview(tree, c, False))

        # Add scrollbars
        v_scrollbar = ttk.Scrollbar(tree_frame, orient=tk.VERTICAL, command=tree.yview)
        v_scrollbar.grid(row=0, column=1, sticky="ns")
        tree.configure(yscrollcommand=v_scrollbar.set)

        h_scrollbar = ttk.Scrollbar(tree_frame, orient=tk.HORIZONTAL, command=tree.xview)
        h_scrollbar.grid(row=1, column=0, sticky="ew")
        tree.configure(xscrollcommand=h_scrollbar.set)

        # Bind events
        tree.bind("<Button-1>", lambda e: self.on_tree_click(e, tree))
        tree.bind("<Double-1>", lambda e: self.on_tree_double_click(e, tree))

        # Store tree reference
        if tab_type == "movies":
            self.movies_tree = tree
        else:
            self.series_tree = tree

    def create_status_bar(self):
        """Create the status bar"""
        self.status_frame = ttk.Frame(self.root)
        self.status_frame.grid(row=2, column=0, sticky="ew", padx=5, pady=2)

        self.status_label = ttk.Label(self.status_frame, text="Ready")
        self.status_label.pack(side=tk.LEFT)

        self.progress_bar = ttk.Progressbar(self.status_frame, mode='indeterminate')
        self.progress_bar.pack(side=tk.RIGHT, padx=10)

    def update_status(self, message: str, show_progress: bool = False):
        """Update status bar"""
        self.status_label.config(text=message)
        if show_progress:
            self.progress_bar.start()
        else:
            self.progress_bar.stop()
        self.root.update_idletasks()

    def load_items(self):
        """Load items from database in a separate thread"""
        def load_worker():
            try:
                self.update_status("Loading items from database...", True)
                self.items_data = self.get_database_items()
                self.root.after(0, self.populate_trees)
            except Exception as e:
                self.root.after(0, lambda: messagebox.showerror("Error", f"Failed to load items: {e}"))
            finally:
                self.root.after(0, lambda: self.update_status("Ready"))

        threading.Thread(target=load_worker, daemon=True).start()

    def get_database_items(self) -> List[Dict]:
        """Get items from the nzbdav database"""
        db_path = self.config.get("database_path")
        if not os.path.exists(db_path):
            raise FileNotFoundError(f"Database file not found: {db_path}")

        items = []
        try:
            with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()

                # Get all file paths from releases
                cursor.execute("""
                    SELECT Path FROM DavItems WHERE Id IN (
                        SELECT Id FROM DavNzbFiles UNION SELECT Id FROM DavRarFiles
                    )
                """)

                # Group by release directory
                release_dirs = {}
                for row in cursor.fetchall():
                    path = row['Path']
                    rel_dir, category, release_name = parse_release_dir(path)
                    if rel_dir and rel_dir not in release_dirs:
                        release_dirs[rel_dir] = {
                            'category': category,
                            'release_name': release_name,
                            'paths': []
                        }
                    if rel_dir:
                        release_dirs[rel_dir]['paths'].append(path)

                # Process each release
                for rel_dir, info in release_dirs.items():
                    category = info['category']
                    release_name = info['release_name']

                    # Determine media type and clean title
                    if is_series(release_name, category):
                        media_type = "series"
                        clean_title = extract_series_title(release_name)
                    elif is_movie(release_name, category):
                        media_type = "movie"
                        clean_title = extract_movie_title(release_name)
                    else:
                        # Skip items we can't classify
                        continue

                    # Check if already processed
                    is_processed = self.status_db.is_processed(clean_title, category, rel_dir)

                    items.append({
                        'title': clean_title,
                        'category': category,
                        'release_name': release_name,
                        'release_path': rel_dir,
                        'media_path': self.find_media_path(clean_title, media_type),
                        'media_type': media_type,
                        'is_processed': is_processed,
                        'selected': False
                    })

        except sqlite3.Error as e:
            raise Exception(f"Database error: {e}")

        return items

    def find_media_path(self, title: str, media_type: str) -> str:
        """Find the actual media file path (placeholder for now)"""
        # This would integrate with Radarr/Sonarr APIs to find actual file locations
        return "Not found"

    def populate_trees(self):
        """Populate the tree views with loaded items"""
        self.apply_filter()

    def apply_filter(self, *args):
        """Apply current filter to items and update trees"""
        filter_text = self.filter_var.get().lower()
        show_processed = self.show_processed_var.get()

        # Filter items
        self.filtered_items = []
        for item in self.items_data:
            # Text filter
            if filter_text and filter_text not in item['title'].lower():
                continue

            # Processed filter
            if not show_processed and item['is_processed']:
                continue

            self.filtered_items.append(item)

        # Update trees
        self.update_trees()

    def update_trees(self):
        """Update tree views with filtered items"""
        # Clear trees
        for tree in [self.movies_tree, self.series_tree]:
            for item in tree.get_children():
                tree.delete(item)

        # Populate trees
        movie_count = 0
        series_count = 0

        for item in self.filtered_items:
            tree = self.movies_tree if item['media_type'] == 'movie' else self.series_tree

            # Status display
            status = "Processed" if item['is_processed'] else "Pending"
            last_processed = "N/A"  # Would get from status_db

            # Checkbox state
            checkbox = "☑" if item['selected'] else "☐"

            values = (
                item['title'],
                item['category'],
                item['release_path'],
                item['media_path'],
                status,
                last_processed
            )

            tree.insert("", "end", text=checkbox, values=values, tags=(status.lower(),))

            if item['media_type'] == 'movie':
                movie_count += 1
            else:
                series_count += 1

        # Configure row colors
        for tree in [self.movies_tree, self.series_tree]:
            tree.tag_configure("processed", background="#e8f5e8")
            tree.tag_configure("pending", background="#fff8dc")

        # Update tab titles with counts
        self.notebook.tab(0, text=f"Movies ({movie_count})")
        self.notebook.tab(1, text=f"TV Series ({series_count})")

    def on_tree_click(self, event, tree):
        """Handle tree item click for checkbox toggle"""
        item = tree.identify_row(event.y)
        if item:
            # Toggle selection
            current_text = tree.item(item, "text")
            new_text = "☑" if current_text == "☐" else "☐"
            tree.item(item, text=new_text)

            # Update item data
            values = tree.item(item, "values")
            title = values[0]
            for data_item in self.filtered_items:
                if data_item['title'] == title:
                    data_item['selected'] = (new_text == "☑")
                    break

    def on_tree_double_click(self, event, tree):
        """Handle tree item double-click"""
        item = tree.identify_row(event.y)
        if item:
            values = tree.item(item, "values")
            title = values[0]
            messagebox.showinfo("Item Details", f"Title: {title}\nPath: {values[2]}")

    def sort_treeview(self, tree, col, reverse):
        """Sort tree view by column"""
        items = [(tree.set(child, col), child) for child in tree.get_children('')]
        items.sort(reverse=reverse)

        for index, (val, child) in enumerate(items):
            tree.move(child, '', index)

        # Update column heading to show sort direction
        tree.heading(col, command=lambda: self.sort_treeview(tree, col, not reverse))

    def select_all(self):
        """Select all visible items"""
        for item in self.filtered_items:
            item['selected'] = True
        self.update_trees()

    def select_none(self):
        """Deselect all items"""
        for item in self.items_data:
            item['selected'] = False
        self.update_trees()

    def get_selected_items(self) -> List[Dict]:
        """Get currently selected items"""
        return [item for item in self.filtered_items if item['selected']]

    def process_selected(self):
        """Process selected items"""
        selected = self.get_selected_items()
        if not selected:
            messagebox.showwarning("No Selection", "Please select items to process.")
            return

        if len(selected) > self.config.get("max_batch_size"):
            messagebox.showwarning("Too Many Items",
                f"Please select no more than {self.config.get('max_batch_size')} items at once.")
            return

        self.process_items(selected)

    def batch_process_all(self):
        """Process all pending items in batches"""
        pending_items = [item for item in self.filtered_items if not item['is_processed']]

        if not pending_items:
            messagebox.showinfo("No Items", "No pending items to process.")
            return

        batch_size = self.config.get("batch_size")
        total_batches = (len(pending_items) + batch_size - 1) // batch_size

        result = messagebox.askyesno("Batch Process",
            f"Process {len(pending_items)} items in {total_batches} batches of {batch_size}?")

        if result:
            self.process_items(pending_items[:batch_size])

    def process_items(self, items: List[Dict]):
        """Process a list of items"""
        if self.processing_thread and self.processing_thread.is_alive():
            messagebox.showwarning("Processing", "Another operation is already in progress.")
            return

        self.processing_cancelled = False
        self.processing_thread = threading.Thread(
            target=self.process_items_worker,
            args=(items,),
            daemon=True
        )
        self.processing_thread.start()

    def process_items_worker(self, items: List[Dict]):
        """Worker thread for processing items"""
        try:
            total = len(items)
            processed = 0

            # Separate movies and series
            movies = [item for item in items if item['media_type'] == 'movie']
            series = [item for item in items if item['media_type'] == 'series']

            # Process movies
            if movies and self.config.get("radarr_url"):
                self.root.after(0, lambda: self.update_status(f"Processing {len(movies)} movies...", True))
                movie_titles = [item['title'] for item in movies]

                try:
                    success = trigger_radarr_searches(
                        movie_titles,
                        self.config.get("radarr_url"),
                        self.config.get("radarr_api_key"),
                        delay=self.config.get("api_delay"),
                        timeout=15.0
                    )

                    # Update status
                    for item in movies:
                        status = 'processed' if item['title'] in success else 'failed'
                        self.status_db.add_processed(
                            item['title'], item['category'], item['release_path'],
                            item['media_type'], status
                        )
                        item['is_processed'] = (status == 'processed')
                        processed += 1

                except Exception as e:
                    for item in movies:
                        self.status_db.add_processed(
                            item['title'], item['category'], item['release_path'],
                            item['media_type'], 'failed', str(e)
                        )

            # Process series
            if series and self.config.get("sonarr_url"):
                self.root.after(0, lambda: self.update_status(f"Processing {len(series)} series...", True))
                series_titles = [item['title'] for item in series]

                try:
                    success = trigger_sonarr_searches(
                        series_titles,
                        self.config.get("sonarr_url"),
                        self.config.get("sonarr_api_key"),
                        delay=self.config.get("api_delay"),
                        timeout=15.0
                    )

                    # Update status
                    for item in series:
                        status = 'processed' if item['title'] in success else 'failed'
                        self.status_db.add_processed(
                            item['title'], item['category'], item['release_path'],
                            item['media_type'], status
                        )
                        item['is_processed'] = (status == 'processed')
                        processed += 1

                except Exception as e:
                    for item in series:
                        self.status_db.add_processed(
                            item['title'], item['category'], item['release_path'],
                            item['media_type'], 'failed', str(e)
                        )

            # Update UI
            self.root.after(0, self.update_trees)
            self.root.after(0, lambda: messagebox.showinfo("Complete", f"Processed {processed}/{total} items."))

        except Exception as e:
            self.root.after(0, lambda: messagebox.showerror("Error", f"Processing failed: {e}"))
        finally:
            self.root.after(0, lambda: self.update_status("Ready"))

    def refresh_items(self):
        """Refresh items from database"""
        self.load_items()

    def change_database(self):
        """Change database file"""
        filename = filedialog.askopenfilename(
            title="Select NZB Database",
            filetypes=[("SQLite files", "*.sqlite *.db"), ("All files", "*.*")]
        )
        if filename:
            self.config.set("database_path", filename)
            self.config.save()
            self.refresh_items()

    def open_settings(self):
        """Open settings dialog"""
        SettingsDialog(self.root, self.config)

    def show_about(self):
        """Show about dialog"""
        messagebox.showinfo("About",
            "NZBDAVMigrator GUI v1.0\n"
            "A tool for managing NZB migrations with Radarr/Sonarr integration.\n\n"
            "Built with Python and Tkinter")

    def on_closing(self):
        """Handle application closing"""
        # Save window geometry
        self.config.set("window_geometry", self.root.geometry())
        self.config.save()

        # Cancel any ongoing processing
        self.processing_cancelled = True

        self.root.destroy()

    def run(self):
        """Start the GUI application"""
        self.root.mainloop()


class SettingsDialog:
    """Settings configuration dialog"""

    def __init__(self, parent, config: Config):
        self.config = config
        self.window = tk.Toplevel(parent)
        self.window.title("Settings")
        self.window.geometry("500x400")
        self.window.transient(parent)
        self.window.grab_set()

        self.setup_dialog()

        # Center on parent
        self.window.update_idletasks()
        x = parent.winfo_x() + (parent.winfo_width() // 2) - (self.window.winfo_width() // 2)
        y = parent.winfo_y() + (parent.winfo_height() // 2) - (self.window.winfo_height() // 2)
        self.window.geometry(f"+{x}+{y}")

    def setup_dialog(self):
        """Setup the settings dialog"""
        notebook = ttk.Notebook(self.window)
        notebook.pack(fill="both", expand=True, padx=10, pady=10)

        # Connection settings tab
        conn_frame = ttk.Frame(notebook)
        notebook.add(conn_frame, text="Connections")
        self.setup_connection_tab(conn_frame)

        # Processing settings tab
        proc_frame = ttk.Frame(notebook)
        notebook.add(proc_frame, text="Processing")
        self.setup_processing_tab(proc_frame)

        # Buttons frame
        button_frame = ttk.Frame(self.window)
        button_frame.pack(fill="x", padx=10, pady=10)

        ttk.Button(button_frame, text="Test Connections",
                  command=self.test_connections).pack(side="left", padx=5)
        ttk.Button(button_frame, text="Cancel",
                  command=self.window.destroy).pack(side="right", padx=5)
        ttk.Button(button_frame, text="Save",
                  command=self.save_settings).pack(side="right", padx=5)

    def setup_connection_tab(self, parent):
        """Setup connection settings tab"""
        # Radarr settings
        radarr_frame = ttk.LabelFrame(parent, text="Radarr Settings")
        radarr_frame.pack(fill="x", padx=10, pady=10)

        ttk.Label(radarr_frame, text="URL:").grid(row=0, column=0, sticky="w", padx=5, pady=5)
        self.radarr_url_var = tk.StringVar(value=self.config.get("radarr_url"))
        ttk.Entry(radarr_frame, textvariable=self.radarr_url_var, width=40).grid(row=0, column=1, padx=5, pady=5)

        ttk.Label(radarr_frame, text="API Key:").grid(row=1, column=0, sticky="w", padx=5, pady=5)
        self.radarr_key_var = tk.StringVar(value=self.config.get("radarr_api_key"))
        ttk.Entry(radarr_frame, textvariable=self.radarr_key_var, width=40, show="*").grid(row=1, column=1, padx=5, pady=5)

        # Sonarr settings
        sonarr_frame = ttk.LabelFrame(parent, text="Sonarr Settings")
        sonarr_frame.pack(fill="x", padx=10, pady=10)

        ttk.Label(sonarr_frame, text="URL:").grid(row=0, column=0, sticky="w", padx=5, pady=5)
        self.sonarr_url_var = tk.StringVar(value=self.config.get("sonarr_url"))
        ttk.Entry(sonarr_frame, textvariable=self.sonarr_url_var, width=40).grid(row=0, column=1, padx=5, pady=5)

        ttk.Label(sonarr_frame, text="API Key:").grid(row=1, column=0, sticky="w", padx=5, pady=5)
        self.sonarr_key_var = tk.StringVar(value=self.config.get("sonarr_api_key"))
        ttk.Entry(sonarr_frame, textvariable=self.sonarr_key_var, width=40, show="*").grid(row=1, column=1, padx=5, pady=5)

    def setup_processing_tab(self, parent):
        """Setup processing settings tab"""
        proc_frame = ttk.LabelFrame(parent, text="Processing Limits")
        proc_frame.pack(fill="x", padx=10, pady=10)

        ttk.Label(proc_frame, text="Batch Size:").grid(row=0, column=0, sticky="w", padx=5, pady=5)
        self.batch_size_var = tk.IntVar(value=self.config.get("batch_size"))
        batch_spin = ttk.Spinbox(proc_frame, from_=1, to=50, textvariable=self.batch_size_var, width=10)
        batch_spin.grid(row=0, column=1, sticky="w", padx=5, pady=5)

        ttk.Label(proc_frame, text="Max Batch Size:").grid(row=1, column=0, sticky="w", padx=5, pady=5)
        self.max_batch_var = tk.IntVar(value=self.config.get("max_batch_size"))
        max_spin = ttk.Spinbox(proc_frame, from_=1, to=100, textvariable=self.max_batch_var, width=10)
        max_spin.grid(row=1, column=1, sticky="w", padx=5, pady=5)

        ttk.Label(proc_frame, text="API Delay (seconds):").grid(row=2, column=0, sticky="w", padx=5, pady=5)
        self.delay_var = tk.DoubleVar(value=self.config.get("api_delay"))
        delay_spin = ttk.Spinbox(proc_frame, from_=0.0, to=10.0, increment=0.5,
                                textvariable=self.delay_var, width=10)
        delay_spin.grid(row=2, column=1, sticky="w", padx=5, pady=5)

    def test_connections(self):
        """Test API connections"""
        results = []

        # Test Radarr
        if self.radarr_url_var.get() and self.radarr_key_var.get():
            try:
                response = _api_request(self.radarr_url_var.get(), self.radarr_key_var.get(),
                                      "api/v3/system/status", timeout=5.0)
                if response:
                    results.append("Radarr: Connected ✓")
                else:
                    results.append("Radarr: Invalid response ✗")
            except Exception as e:
                results.append(f"Radarr: Failed - {e} ✗")
        else:
            results.append("Radarr: Not configured")

        # Test Sonarr
        if self.sonarr_url_var.get() and self.sonarr_key_var.get():
            try:
                response = _api_request(self.sonarr_url_var.get(), self.sonarr_key_var.get(),
                                      "api/v3/system/status", timeout=5.0)
                if response:
                    results.append("Sonarr: Connected ✓")
                else:
                    results.append("Sonarr: Invalid response ✗")
            except Exception as e:
                results.append(f"Sonarr: Failed - {e} ✗")
        else:
            results.append("Sonarr: Not configured")

        messagebox.showinfo("Connection Test Results", "\n".join(results))

    def save_settings(self):
        """Save settings and close dialog"""
        # Validate settings
        if self.batch_size_var.get() > self.max_batch_var.get():
            messagebox.showerror("Invalid Settings", "Batch size cannot be larger than max batch size.")
            return

        # Save to config
        self.config.set("radarr_url", self.radarr_url_var.get().strip())
        self.config.set("radarr_api_key", self.radarr_key_var.get().strip())
        self.config.set("sonarr_url", self.sonarr_url_var.get().strip())
        self.config.set("sonarr_api_key", self.sonarr_key_var.get().strip())
        self.config.set("batch_size", self.batch_size_var.get())
        self.config.set("max_batch_size", self.max_batch_var.get())
        self.config.set("api_delay", self.delay_var.get())

        self.config.save()
        messagebox.showinfo("Settings", "Settings saved successfully!")
        self.window.destroy()


def main():
    """Main entry point"""
    try:
        app = NZBDAVMigratorGUI()
        app.run()
    except Exception as e:
        messagebox.showerror("Error", f"Failed to start application: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()