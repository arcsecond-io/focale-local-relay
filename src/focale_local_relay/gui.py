from __future__ import annotations

import json
import sys
import traceback
from datetime import datetime
from threading import Event
from typing import Any, Callable

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Qt, Signal, Slot
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QStatusBar,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from . import __version__
from . import branding
from . import services
from ._environment import ENVIRONMENT as BAKED_ENVIRONMENT
from .exceptions import FocaleError, HubGatewayError

WorkerFunc = Callable[[Callable[[str], None]], object]


class WorkerSignals(QObject):
    result = Signal(object)
    error = Signal(str)
    log = Signal(str)
    finished = Signal()


class FunctionWorker(QRunnable):
    def __init__(self, fn: WorkerFunc) -> None:
        super().__init__()
        self.setAutoDelete(False)
        self.fn = fn
        self.signals = WorkerSignals()

    def run(self) -> None:
        try:
            result = self.fn(self.signals.log.emit)
        except Exception as exc:
            if isinstance(exc, (FocaleError, HubGatewayError)):
                self.signals.error.emit(str(exc))
            else:
                self.signals.error.emit(traceback.format_exc())
        else:
            self.signals.result.emit(result)
        finally:
            self.signals.finished.emit()


class FocaleWindow(QMainWindow):
    relay_message_signal = Signal(object)
    relay_started_signal = Signal(object)
    relay_stopped_signal = Signal(object)

    def __init__(self) -> None:
        super().__init__()
        self.thread_pool = QThreadPool(self)
        self._busy_count = 0
        self._active_workers: dict[
            int, tuple[FunctionWorker, str, Callable[[Any], None] | None]
        ] = {}
        self._settings = services.user_settings()
        self._relay_stop_event: Event | None = None
        self._relay_running = False

        self.relay_message_signal.connect(self._append_message_event)
        self.relay_started_signal.connect(self._handle_relay_started)
        self.relay_stopped_signal.connect(self._handle_relay_stopped)

        self.setWindowTitle(branding.window_title(__version__, BAKED_ENVIRONMENT))
        self.resize(980, 760)
        self.setStatusBar(QStatusBar(self))
        self._apply_window_icon()

        root = QWidget(self)
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        heading_row = QHBoxLayout()
        heading_row.setSpacing(10)

        icon_label = self._build_heading_icon_label()
        if icon_label is not None:
            heading_row.addWidget(icon_label, 0, Qt.AlignVCenter)

        heading = QLabel(branding.display_name(BAKED_ENVIRONMENT))
        heading.setStyleSheet("font-size: 24px; font-weight: 600;")
        heading_row.addWidget(heading, 0, Qt.AlignVCenter)
        heading_row.addStretch(1)
        layout.addLayout(heading_row)

        subheading = QLabel(branding.APP_DESCRIPTION)
        subheading.setWordWrap(True)
        layout.addWidget(subheading)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_account_tab(), "Account")
        self.tabs.addTab(self._build_alpaca_tab(), "Alpaca Server")
        self.tabs.addTab(self._build_platesolver_tab(), "Plate Solver")
        self.messages_tab = self._build_messages_tab()
        self.tabs.addTab(self.messages_tab, "Messages")
        layout.addWidget(self.tabs, 1)

        self.log_output = QPlainTextEdit()
        self.log_output.setReadOnly(True)
        self.log_output.setPlaceholderText("Action logs and JSON results will appear here.")
        self.log_output.setMinimumHeight(180)
        layout.addWidget(self.log_output)

        self._refresh_status_summary()

    # ------------------------------------------------------------------ #
    # Tab builders                                                         #
    # ------------------------------------------------------------------ #

    def _build_account_tab(self) -> QWidget:
        tab = QWidget()
        layout = QGridLayout(tab)
        layout.setSpacing(12)
        layout.setColumnStretch(0, 3)
        layout.setColumnStretch(1, 2)

        # --- Focale Account ---
        account_box = QGroupBox(branding.ACCOUNT_GROUP_TITLE)
        account_form = QFormLayout(account_box)
        account_form.setFieldGrowthPolicy(QFormLayout.ExpandingFieldsGrow)
        account_form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)

        self.username_input = QLineEdit(self._settings.get("username") or "")
        self.username_input.setMinimumWidth(220)
        self.secret_input = QLineEdit()
        self.secret_input.setMinimumWidth(220)
        self.secret_input.setEchoMode(QLineEdit.Password)
        self.secret_input.setPlaceholderText("Only needed when signing in again")
        self.environment_label = QLabel(
            str(
                self._settings.get("environment_label")
                or branding.default_environment_label(BAKED_ENVIRONMENT)
            )
        )
        account_form.addRow("Username", self.username_input)
        account_form.addRow("Password", self.secret_input)
        account_form.addRow("Environment", self.environment_label)

        account_buttons = QHBoxLayout()
        login_button = QPushButton("Sign In")
        login_button.clicked.connect(self._login)
        refresh_button = QPushButton("Refresh State")
        refresh_button.clicked.connect(self._refresh_status)
        account_buttons.addWidget(login_button)
        account_buttons.addWidget(refresh_button)
        account_buttons.addStretch(1)
        account_form.addRow("", account_buttons)

        account_note = QLabel(
            f"{branding.APP_NAME} keeps your session locally, so you usually only need to sign in once."
        )
        account_note.setWordWrap(True)
        account_form.addRow("", account_note)
        layout.addWidget(account_box, 0, 0)

        # --- Hub ---
        hub_box = QGroupBox("Hub")
        hub_form = QFormLayout(hub_box)
        hub_form.setFieldGrowthPolicy(QFormLayout.ExpandingFieldsGrow)
        hub_form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        hub_note = QLabel(
            f"The Hub connection follows your {branding.APP_NAME} environment automatically."
        )
        hub_note.setWordWrap(True)
        hub_form.addRow("", hub_note)

        hub_buttons = QHBoxLayout()
        doctor_button = QPushButton("Run Doctor")
        doctor_button.clicked.connect(self._doctor)
        connect_button = QPushButton("Connect Once")
        connect_button.clicked.connect(self._connect_once)
        hub_buttons.addWidget(doctor_button)
        hub_buttons.addWidget(connect_button)
        hub_buttons.addStretch(1)
        hub_form.addRow("", hub_buttons)
        layout.addWidget(hub_box, 1, 0)

        # --- State table (right column, spans both rows) ---
        status_box = QGroupBox("State")
        status_layout = QVBoxLayout(status_box)
        self.state_table = QTableWidget(0, 2)
        self.state_table.setHorizontalHeaderLabels(["Property", "Value"])
        self.state_table.verticalHeader().setVisible(False)
        self.state_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.state_table.setSelectionMode(QTableWidget.NoSelection)
        self.state_table.horizontalHeader().setStretchLastSection(True)
        self.state_table.horizontalHeader().setDefaultAlignment(Qt.AlignLeft)
        self.state_table.setMinimumWidth(300)
        status_layout.addWidget(self.state_table)
        layout.addWidget(status_box, 0, 1, 2, 1)

        layout.setRowStretch(2, 1)

        return tab

    def _build_alpaca_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(12)

        alpaca_box = QGroupBox("Local Alpaca Servers")
        alpaca_form = QFormLayout(alpaca_box)
        alpaca_form.setFieldGrowthPolicy(QFormLayout.ExpandingFieldsGrow)

        self.local_alpaca_summary = QLabel("No local scan yet.")
        self.local_alpaca_summary.setWordWrap(True)
        alpaca_form.addRow("", self.local_alpaca_summary)

        alpaca_buttons = QHBoxLayout()
        discover_alpaca_button = QPushButton("Check Local Alpaca Servers")
        discover_alpaca_button.clicked.connect(self._discover_local_alpaca)
        register_alpaca_button = QPushButton(f"Register Server To {branding.APP_NAME}")
        register_alpaca_button.clicked.connect(self._register_local_alpaca)
        alpaca_buttons.addWidget(discover_alpaca_button)
        alpaca_buttons.addWidget(register_alpaca_button)
        alpaca_buttons.addStretch(1)
        alpaca_form.addRow("", self._wrap_layout(alpaca_buttons))
        layout.addWidget(alpaca_box)
        layout.addStretch(1)

        return tab

    def _build_platesolver_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(12)

        # Load persisted config
        centering_config = services.get_centering_config()

        # --- Centering parameters ---
        centering_box = QGroupBox("Centering Parameters")
        centering_outer = QVBoxLayout(centering_box)
        centering_outer.setSpacing(8)

        centering_note = QLabel(
            "These parameters are used when the Hub triggers a centering procedure. "
            "Camera, telescope, and target coordinates are resolved automatically "
            "from your registered Alpaca equipment."
        )
        centering_note.setWordWrap(True)
        centering_note.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        centering_outer.addWidget(centering_note)

        # 2-column grid: (label | field) (label | field)
        centering_grid = QGridLayout()
        centering_grid.setHorizontalSpacing(16)
        centering_grid.setVerticalSpacing(8)
        centering_grid.setColumnStretch(1, 1)
        centering_grid.setColumnStretch(3, 1)

        def _add_pair(row, col, label_text, widget):
            lbl = QLabel(label_text)
            lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            centering_grid.addWidget(lbl, row, col * 2)
            centering_grid.addWidget(widget, row, col * 2 + 1)

        self.centering_duration_input = QLineEdit(str(centering_config.get("duration", 5.0)))
        self.centering_max_iter_input = QLineEdit(str(centering_config.get("max_iterations", 10)))
        self.centering_min_peaks_input = QLineEdit(str(centering_config.get("min_peaks", 20)))
        self.centering_success_input = QLineEdit(str(centering_config.get("success_threshold", 10.0)))
        self.centering_failure_input = QLineEdit(str(centering_config.get("failure_threshold", 300.0)))
        self.centering_max_dur_adj_input = QLineEdit(str(centering_config.get("max_duration_adjustments", 2)))

        _add_pair(0, 0, "Exposure (s)", self.centering_duration_input)
        _add_pair(0, 1, "Max iterations", self.centering_max_iter_input)
        _add_pair(1, 0, "Min peaks", self.centering_min_peaks_input)
        _add_pair(1, 1, 'Success threshold (")', self.centering_success_input)
        _add_pair(2, 0, 'Failure threshold (")', self.centering_failure_input)
        _add_pair(2, 1, "Max exposure doublings", self.centering_max_dur_adj_input)

        centering_outer.addLayout(centering_grid)
        layout.addWidget(centering_box)

        # --- Local solver index files ---
        solver_box = QGroupBox("Local Solver Index Files")
        solver_form = QFormLayout(solver_box)
        solver_form.setFieldGrowthPolicy(QFormLayout.ExpandingFieldsGrow)
        solver_form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)

        self.solver_cache_dir_input = QLineEdit(str(centering_config.get("cache_dir") or ""))
        cache_row = QHBoxLayout()
        cache_row.addWidget(self.solver_cache_dir_input, 1)
        browse_cache_button = QPushButton("Browse")
        browse_cache_button.clicked.connect(self._browse_cache_dir)
        cache_row.addWidget(browse_cache_button)
        solver_form.addRow("Cache directory", self._wrap_layout(cache_row))
        layout.addWidget(solver_box)

        # --- Buttons ---
        button_row = QHBoxLayout()
        check_solver_button = QPushButton("Check Solver")
        check_solver_button.clicked.connect(self._platesolver_status)
        save_settings_button = QPushButton("Save Settings")
        save_settings_button.clicked.connect(self._save_centering_settings)
        button_row.addWidget(check_solver_button)
        button_row.addWidget(save_settings_button)
        button_row.addStretch(1)
        layout.addLayout(button_row)
        layout.addStretch(1)

        return tab

    def _build_messages_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(12)

        info = QLabel(
            "Live relay traffic between the remote Hub and your local relay session. "
            "Incoming Hub frames use an inbound arrow, outbound responses and local progress use an outbound arrow."
        )
        info.setWordWrap(True)
        layout.addWidget(info)

        controls = QHBoxLayout()
        self.relay_status_label = QLabel("Relay stopped.")
        self.start_relay_button = QPushButton("Start Relay")
        self.stop_relay_button = QPushButton("Stop Relay")
        clear_button = QPushButton("Clear")

        self.start_relay_button.clicked.connect(self._start_relay)
        self.stop_relay_button.clicked.connect(self._stop_relay)
        clear_button.clicked.connect(self._clear_messages)
        self.stop_relay_button.setEnabled(False)

        controls.addWidget(self.relay_status_label)
        controls.addStretch(1)
        controls.addWidget(self.start_relay_button)
        controls.addWidget(self.stop_relay_button)
        controls.addWidget(clear_button)
        layout.addLayout(controls)

        self.messages_table = QTableWidget(0, 5)
        self.messages_table.setHorizontalHeaderLabels(
            ["Time", "Dir", "Channel", "Type", "Summary"]
        )
        self.messages_table.verticalHeader().setVisible(False)
        self.messages_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.messages_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.messages_table.setSelectionMode(QTableWidget.SingleSelection)
        self.messages_table.horizontalHeader().setStretchLastSection(True)
        self.messages_table.setMinimumHeight(220)
        self.messages_table.itemSelectionChanged.connect(self._show_selected_message)
        layout.addWidget(self.messages_table, 1)

        self.message_details = QPlainTextEdit()
        self.message_details.setReadOnly(True)
        self.message_details.setPlaceholderText("Select a message to inspect its full payload.")
        self.message_details.setMinimumHeight(180)
        layout.addWidget(self.message_details)

        return tab

    # ------------------------------------------------------------------ #
    # Helpers                                                              #
    # ------------------------------------------------------------------ #

    def _apply_window_icon(self) -> None:
        icon_path = branding.find_window_icon_path()
        if icon_path is None:
            return

        icon = QIcon(str(icon_path))
        if icon.isNull():
            return

        self.setWindowIcon(icon)
        app = QApplication.instance()
        if app is not None:
            app.setWindowIcon(icon)

    def _build_heading_icon_label(self) -> QLabel | None:
        icon_path = branding.find_window_icon_path()
        if icon_path is None:
            return None

        pixmap = QPixmap(str(icon_path))
        if pixmap.isNull():
            return None

        label = QLabel()
        label.setPixmap(
            pixmap.scaled(32, 32, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        )
        label.setFixedSize(32, 32)
        return label

    def _wrap_layout(self, layout) -> QWidget:
        widget = QWidget()
        widget.setLayout(layout)
        return widget

    def _start_action(
        self,
        label: str,
        fn: WorkerFunc,
        *,
        on_result: Callable[[Any], None] | None = None,
    ) -> None:
        worker = FunctionWorker(fn)
        self._active_workers[id(worker.signals)] = (worker, label, on_result)
        worker.signals.log.connect(self._append_log)
        worker.signals.error.connect(self._handle_worker_error)
        worker.signals.result.connect(self._handle_worker_result)
        worker.signals.finished.connect(self._finish_action)
        self._busy_count += 1
        self.statusBar().showMessage(f"{label} in progress...")
        self.thread_pool.start(worker)

    def _worker_context(
        self,
    ) -> tuple[FunctionWorker, str, Callable[[Any], None] | None] | None:
        sender = self.sender()
        if sender is None:
            return None
        return self._active_workers.get(id(sender))

    @Slot(object)
    def _handle_worker_result(self, payload: Any) -> None:
        context = self._worker_context()
        if context is None:
            return
        _worker, label, on_result = context
        self._append_log(f"{label} completed.")
        self._append_log(self._format_payload(payload))
        if on_result is not None:
            on_result(payload)

    @Slot(str)
    def _handle_worker_error(self, message: str) -> None:
        context = self._worker_context()
        if context is None:
            return
        _worker, label, _on_result = context
        self._handle_error(label, message)

    def _handle_error(self, label: str, message: str) -> None:
        self._append_log(f"{label} failed.")
        self._append_log(message)
        self._refresh_status_summary()
        if label == "Relay":
            self.relay_stopped_signal.emit({})
        QMessageBox.critical(self, branding.APP_NAME, f"{label} failed.\n\n{message}")

    @Slot()
    def _finish_action(self) -> None:
        sender = self.sender()
        if sender is not None:
            self._active_workers.pop(id(sender), None)
        self._busy_count = max(0, self._busy_count - 1)
        if self._busy_count == 0:
            self.statusBar().showMessage("Ready")

    @Slot(str)
    def _append_log(self, message: str) -> None:
        self.log_output.appendPlainText(message)

    def _format_payload(self, payload: Any) -> str:
        return json.dumps(payload, indent=2, sort_keys=True, default=str)

    def _message_direction_symbol(self, direction: str) -> str:
        if direction == "incoming":
            return "↘"
        if direction in {"outgoing", "local"}:
            return "↗"
        return "•"

    def _message_time(self) -> str:
        return datetime.now().strftime("%H:%M:%S")

    def _api_server(self) -> str | None:
        value = str(self._settings.get("api_server") or "").strip()
        return value or None

    def _hub_url(self) -> str | None:
        value = str(self._settings.get("hub_url") or "").strip()
        return value or None

    def _clear_messages(self) -> None:
        self.messages_table.setRowCount(0)
        self.message_details.clear()

    @Slot(object)
    def _append_message_event(self, event: dict[str, Any]) -> None:
        row = self.messages_table.rowCount()
        self.messages_table.insertRow(row)

        payload = event.get("payload") or {}
        serialized = self._format_payload(payload)
        values = [
            self._message_time(),
            self._message_direction_symbol(str(event.get("direction") or "")),
            str(event.get("channel") or ""),
            str(event.get("message_type") or ""),
            str(event.get("summary") or ""),
        ]
        for column, value in enumerate(values):
            item = QTableWidgetItem(value)
            item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
            item.setData(Qt.UserRole, serialized)
            self.messages_table.setItem(row, column, item)

        self.messages_table.scrollToBottom()
        self.messages_table.selectRow(row)

    def _show_selected_message(self) -> None:
        selected = self.messages_table.selectedItems()
        if not selected:
            self.message_details.clear()
            return
        self.message_details.setPlainText(str(selected[0].data(Qt.UserRole) or ""))

    def _set_relay_controls(self, *, running: bool, status: str) -> None:
        self._relay_running = running
        self.relay_status_label.setText(status)
        self.start_relay_button.setEnabled(not running)
        self.stop_relay_button.setEnabled(running)

    @Slot(object)
    def _handle_relay_started(self, payload: dict[str, Any]) -> None:
        session_id = str(payload.get("session_id") or "")
        self._set_relay_controls(
            running=True,
            status=f"Relay running. session_id={session_id}" if session_id else "Relay running.",
        )

    @Slot(object)
    def _handle_relay_stopped(self, payload: dict[str, Any]) -> None:
        session_id = str(payload.get("session_id") or "")
        suffix = f" Last session: {session_id}." if session_id else ""
        self._set_relay_controls(running=False, status=f"Relay stopped.{suffix}")
        self._relay_stop_event = None

    # ------------------------------------------------------------------ #
    # Account tab actions                                                  #
    # ------------------------------------------------------------------ #

    def _login(self) -> None:
        api_server = self._api_server()
        username = self.username_input.text().strip()
        secret = self.secret_input.text()
        if not username or not secret:
            QMessageBox.warning(
                self, branding.APP_NAME, "Username and password are required."
            )
            return

        self._start_action(
            "Sign in",
            lambda _log: services.login(
                api_server=api_server,
                username=username,
                secret=secret,
            ),
            on_result=self._after_login,
        )

    def _after_login(self, _payload: Any) -> None:
        self.secret_input.clear()
        self._settings = services.user_settings()
        self.environment_label.setText(
            str(
                self._settings.get("environment_label")
                or branding.default_environment_label(BAKED_ENVIRONMENT)
            )
        )
        self._refresh_status_summary()

    def _refresh_status(self) -> None:
        api_server = self._api_server()
        self._start_action(
            "Refresh state",
            lambda _log: services.status(
                api_server=api_server,
            ),
            on_result=self._set_status_summary,
        )

    def _refresh_status_summary(self) -> None:
        try:
            payload = services.status(
                api_server=self._api_server(),
            )
        except Exception as exc:
            self.state_table.setRowCount(1)
            label_item = QTableWidgetItem("Status")
            value_item = QTableWidgetItem(str(exc))
            label_item.setFlags(Qt.ItemIsEnabled)
            value_item.setFlags(Qt.ItemIsEnabled)
            self.state_table.setItem(0, 0, label_item)
            self.state_table.setItem(0, 1, value_item)
        else:
            self._set_status_summary(payload)

    def _set_status_summary(self, payload: Any) -> None:
        rows = [
            ("Signed in", "Yes" if payload.get("logged_in") else "No"),
            ("Username", str(payload.get("username") or "Not signed in")),
            (
                "Environment",
                str(
                    payload.get("environment_label")
                    or branding.default_environment_label(BAKED_ENVIRONMENT)
                ),
            ),
            ("Stored installations", str(len(payload.get("installations") or {}))),
            ("Known Alpaca servers", str(payload.get("known_alpaca_servers") or 0)),
            (f"{branding.APP_NAME} version", str(payload.get("focale_version") or __version__)),
        ]
        auth_error = payload.get("auth_error")
        if auth_error:
            rows.insert(1, ("Session", str(auth_error)))
        self.state_table.setRowCount(len(rows))
        for row_index, (label, value) in enumerate(rows):
            label_item = QTableWidgetItem(label)
            value_item = QTableWidgetItem(value)
            label_item.setFlags(Qt.ItemIsEnabled)
            value_item.setFlags(Qt.ItemIsEnabled)
            self.state_table.setItem(row_index, 0, label_item)
            self.state_table.setItem(row_index, 1, value_item)

    def _doctor(self) -> None:
        api_server = self._api_server()
        hub_url = self._hub_url()
        self._start_action(
            "Doctor",
            lambda log: services.doctor(
                api_server=api_server,
                hub_url=hub_url,
                organisation=None,
                workspace_id=None,
                force_refresh=False,
                re_enroll=False,
                echo=log,
            ),
        )

    def _connect_once(self) -> None:
        api_server = self._api_server()
        hub_url = self._hub_url()
        self._start_action(
            "Connect once",
            lambda log: services.connect_once(
                api_server=api_server,
                hub_url=hub_url,
                organisation=None,
                workspace_id=None,
                re_enroll=False,
                discover_alpaca=False,
                echo=log,
            ),
        )

    def _start_relay(self) -> None:
        if self._relay_running:
            return

        api_server = self._api_server()
        hub_url = self._hub_url()
        self._relay_stop_event = Event()
        self._set_relay_controls(running=True, status="Starting relay...")
        self.tabs.setCurrentWidget(self.messages_tab)

        def run_relay(log: Callable[[str], None]) -> dict[str, Any]:
            def on_traffic(event: dict[str, Any]) -> None:
                payload = dict(event)
                if payload.get("message_type") == "welcome":
                    self.relay_started_signal.emit(payload.get("payload") or {})
                self.relay_message_signal.emit(payload)

            return services.relay_messages(
                api_server=api_server,
                hub_url=hub_url,
                organisation=None,
                workspace_id=None,
                re_enroll=False,
                discover_alpaca=False,
                echo=log,
                traffic_callback=on_traffic,
                stop_event=self._relay_stop_event,
            )

        self._start_action("Relay", run_relay, on_result=self.relay_stopped_signal.emit)

    def _stop_relay(self) -> None:
        if self._relay_stop_event is None:
            return
        self.relay_status_label.setText("Stopping relay...")
        self._relay_stop_event.set()

    # ------------------------------------------------------------------ #
    # Alpaca tab actions                                                   #
    # ------------------------------------------------------------------ #

    def _discover_local_alpaca(self) -> None:
        self._start_action(
            "Check local Alpaca servers",
            lambda _log: services.discover_local_alpaca(),
            on_result=self._set_local_alpaca_summary,
        )

    def _register_local_alpaca(self) -> None:
        api_server = self._api_server()
        self._start_action(
            "Register local Alpaca servers",
            lambda log: services.register_local_alpaca(
                api_server=api_server,
                echo=log,
            ),
            on_result=self._after_register_local_alpaca,
        )

    def _after_register_local_alpaca(self, payload: Any) -> None:
        self._set_local_alpaca_summary(payload)
        self._refresh_status_summary()

    def _set_local_alpaca_summary(self, payload: Any) -> None:
        count = int(payload.get("count", payload.get("discovered", 0)) or 0)
        if count == 0:
            self.local_alpaca_summary.setText("No local Alpaca servers found.")
            return

        servers = payload.get("servers") or []
        names = ", ".join(
            str(server.get("name") or server.get("address") or "Unknown")
            for server in servers[:3]
            if isinstance(server, dict)
        )
        extra = ""
        if len(servers) > 3:
            extra = f" and {len(servers) - 3} more"

        registration = ""
        if "registered" in payload or "already_registered" in payload:
            registration = (
                f" Registered: {payload.get('registered', 0)},"
                f" already registered: {payload.get('already_registered', 0)}."
            )

        devices = ""
        if "devices_registered" in payload or "devices_already_registered" in payload:
            devices = (
                f" Devices: {payload.get('devices_registered', 0)} new,"
                f" {payload.get('devices_already_registered', 0)} already known."
            )

        resources = ""
        if (
            "sites_created" in payload
            or "telescopes_created" in payload
            or "equipments_created" in payload
        ):
            resources = (
                f" Resources: {payload.get('sites_created', 0)} site(s),"
                f" {payload.get('telescopes_created', 0)} telescope(s),"
                f" {payload.get('equipments_created', 0)} equipment item(s) created."
            )

        self.local_alpaca_summary.setText(
            f"Found {count} local server(s): {names}{extra}.{registration}{devices}{resources}"
        )

    # ------------------------------------------------------------------ #
    # Plate Solver tab actions                                             #
    # ------------------------------------------------------------------ #

    def _platesolver_status(self) -> None:
        cache_dir = self._clean(self.solver_cache_dir_input)
        self._start_action(
            "Check solver",
            lambda _log: services.platesolver_status(
                cache_dir=cache_dir,
                scales="6",
            ),
        )

    def _save_centering_settings(self) -> None:
        try:
            duration = self._required_float(self.centering_duration_input, "Exposure")
            max_iter = self._required_int(self.centering_max_iter_input, "Max iterations")
            min_peaks = self._required_int(self.centering_min_peaks_input, "Min peaks")
            success_thr = self._required_float(self.centering_success_input, "Success threshold")
            failure_thr = self._required_float(self.centering_failure_input, "Failure threshold")
            max_dur_adj = self._required_int(self.centering_max_dur_adj_input, "Max exposure doublings")
        except FocaleError as exc:
            QMessageBox.warning(self, branding.APP_NAME, str(exc))
            return

        cache_dir = self._clean(self.solver_cache_dir_input)
        self._start_action(
            "Save centering settings",
            lambda _log: services.save_centering_config(
                duration=duration,
                max_iterations=max_iter,
                min_peaks=min_peaks,
                success_threshold=success_thr,
                failure_threshold=failure_thr,
                max_duration_adjustments=max_dur_adj,
                cache_dir=cache_dir,
            ),
        )

    def _browse_cache_dir(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Select cache directory")
        if path:
            self.solver_cache_dir_input.setText(path)

    # ------------------------------------------------------------------ #
    # Input helpers                                                        #
    # ------------------------------------------------------------------ #

    def _clean(self, line_edit: QLineEdit) -> str | None:
        value = line_edit.text().strip()
        return value or None

    def _required_float(self, line_edit: QLineEdit, label: str) -> float:
        value = line_edit.text().strip()
        try:
            return float(value)
        except ValueError as exc:
            raise FocaleError(f"{label}: '{value}' is not a valid number.") from exc

    def _required_int(self, line_edit: QLineEdit, label: str) -> int:
        value = line_edit.text().strip()
        try:
            return int(value)
        except ValueError as exc:
            raise FocaleError(f"{label}: '{value}' is not a valid integer.") from exc


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationDisplayName(branding.display_name(BAKED_ENVIRONMENT))
    icon_path = branding.find_window_icon_path()
    if icon_path is not None:
        icon = QIcon(str(icon_path))
        if not icon.isNull():
            app.setWindowIcon(icon)
    services.ensure_environment()
    window = FocaleWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
