
"""TUI Approval Interface for NEXUS."""

import os
import sys
from typing import Any
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.prompt import Prompt, Confirm
from rich.text import Text
from rich.theme import Theme

from .approval_api import ApprovalAPI, APIUser
from .approval import ApprovalManager

# NEXUS Theme: Dark, Emerald/Teal
NEXUS_THEME = Theme({
    "info": "cyan",
    "warning": "yellow",
    "danger": "bold red",
    "success": "bold green",
    "accent": "bold cyan",
    "muted": "grey50"
})

console = Console(theme=NEXUS_THEME)

class ApprovalUI:
    """Terminal User Interface for human authorization."""

    def __init__(self, api: ApprovalAPI, current_user: APIUser) -> None:
        self.api = api
        self.user = current_user

    def clear(self) -> None:
        os.system("clear")

    def display_header(self) -> None:
        self.clear()
        console.print(Panel(
            Text("NEXUS — HUMAN AUTHORIZATION GATE", style="accent", justify="center"),
            style="accent",
            subtitle=f"User: {self.user.name} ({self.user.role})"
        ))

    def list_requests(self) -> None:
        self.display_header()
        pending = self.api.list_pending()
        
        if not pending:
            console.print("[muted]No pending authorization requests.[/muted]")
            return

        table = Table(title="Pending Requests", expand=True, border_style="accent")
        table.add_column("ID", style="muted")
        table.add_column("Tool", style="info")
        table.add_column("Justification", style="white")
        table.add_column("Status", style="yellow")

        for req in pending:
            # We have to extract just the ID for the table
            rid = req["id"]
            tool = req["tool_id"]
            just = req["justification"]
            status = req["status"]
            table.add_row(rid[:8], tool, just, status)

        console.print(table)
        console.print("\n[accent]Commands:[/accent] [bold]view <id>[/] | [bold]approve <id>[/] | [bold]deny <id>[/] | [bold]quit[/]")

    def show_details(self, request_id: str) -> None:
        self.display_header()
        details = self.api.get_details(request_id)
        if not details:
            console.print("[danger]Request not found.[/danger]")
            return

        panel_content = Text()
        panel_content.append(f"Tool ID: {details['tool_id']}\n", style="info")
        panel_content.append(f"Task ID: {details['task_id']}\n", style="muted")
        panel_content.append(f"Plan ID: {details['plan_id']}\n", style="muted")
        panel_content.append(f"Execution Mode: {details['execution_mode']}\n", style="accent")
        panel_content.append(f"Access Level: {details['access_level']}\n", style="accent")
        panel_content.append(f"Filesystem Scope: {details['fs_scope']}\n", style="info")
        panel_content.append(f"Network Scope: {details['net_scope']}\n", style="info")
        panel_content.append(f"Resource Limits: {details['resource_limits']}\n", style="muted")
        panel_content.append(f"Policy Version: {details['policy_version']}\n", style="muted")
        panel_content.append(f"Justification: {details['justification']}\n", style="white")
        panel_content.append(f"Created: {details['created_at']}\n", style="muted")
        panel_content.append(f"Expires: {details['expires_at']}\n", style="warning")

        console.print(Panel(panel_content, title="Request Details", border_style="accent"))
        
        if Confirm.ask("\nDo you authorize this action?"):
            if self.api.approve(request_id, self.user):
                console.print("[success]Request APPROVED.[/success]")
            else:
                console.print("[danger]Approval failed.[/danger]")
        else:
            if self.api.deny(request_id, self.user):
                console.print("[warning]Request DENIED.[/warning]")
            else:
                console.print("[danger]Denial failed.[/danger]")

    def run_loop(self) -> None:
        while True:
            self.list_requests()
            cmd_input = Prompt.ask("\n[accent]nexus-auth>[/]")
            
            if not cmd_input: continue
            parts = cmd_input.split()
            cmd = parts[0].lower()
            
            if cmd == "quit":
                break
            elif cmd == "view" and len(parts) > 1:
                # We need the full ID. For simplicity, we'll look up by prefix.
                prefix = parts[1]
                pending = self.api.list_pending()
                match = next((r for r in pending if r["id"].startswith(prefix)), None)
                if match:
                    self.show_details(match["id"])
                else:
                    console.print("[danger]No matching request found.[/danger]")
            elif cmd == "approve" and len(parts) > 1:
                prefix = parts[1]
                pending = self.api.list_pending()
                match = next((r for r in pending if r["id"].startswith(prefix)), None)
                if match:
                    if self.api.approve(match["id"], self.user):
                        console.print("[success]Approved.[/success]")
                    else:
                        console.print("[danger]Failed.[/danger]")
                else:
                    console.print("[danger]No matching request found.[/danger]")
            elif cmd == "deny" and len(parts) > 1:
                prefix = parts[1]
                pending = self.api.list_pending()
                match = next((r for r in pending if r["id"].startswith(prefix)), None)
                if match:
                    if self.api.deny(match["id"], self.user):
                        console.print("[warning]Denied.[/warning]")
                    else:
                        console.print("[danger]Failed.[/danger]")
                else:
                    console.print("[danger]No matching request found.[/danger]")
            else:
                console.print("[danger]Unknown command.[/danger]")
            
            Prompt.ask("\nPress Enter to return...")

if __name__ == "__main__":
    # Mock setup for standalone testing
    mgr = ApprovalManager()
    api = ApprovalAPI(mgr)
    user = APIUser("admin", "NEXUS Administrator", "SUPERUSER")
    ui = ApprovalUI(api, user)
    ui.run_loop()
