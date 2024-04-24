import argparse
import os
import re
from typing import Dict, Optional

import requests
from requests.exceptions import RequestException
from requests.models import Response


class UpdateJira:
    def __init__(self, jira_token: str, ticket_id: str) -> None:
        self.jira_domain: str = "jira.aledade.com"
        self.jira_token: str = jira_token
        self.ticket_id: str = self.get_jira_ticket_id(ticket_id.lower())

    def get(self, url: str) -> Optional[Response]:
        headers: Dict[str, str] = {
            "Authorization": f"Bearer {self.jira_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        try:
            response: Response = requests.get(url, headers=headers)
            response.raise_for_status()
            print("Response status for get", response.status_code)
            return response
        except RequestException as e:
            print(f"Error with response: {e}")
            return None

    def post(self, url: str, data: Dict) -> Optional[Response]:
        headers: Dict[str, str] = {
            "Authorization": f"Bearer {self.jira_token}",
            "Content-Type": "application/json",
        }
        try:
            response: Response = requests.post(url, headers=headers, json=data)
            response.raise_for_status()
            print("Response status for post", response.status_code)
            return response
        except RequestException as e:
            print(f"Error with response: {e}")
            return None

    def get_ticket_info(self) -> Optional[Dict[str, str]]:
        url: str = f"https://{self.jira_domain}/rest/api/2/issue/{self.ticket_id}"
        response: Optional[Response] = self.get(url)
        try:
            data = response.json()
            issuetype: str = data["fields"]["issuetype"]["name"].lower()
            status: str = data["fields"]["status"]["name"].lower()
            return {"issue_name": issuetype, "status": status}
        except Exception as e:
            print('Error', e)

    def has_ticket_issue(self, ticket_info: Dict[str, str]) -> bool:
        possible_ticket_issues = ["story", "bug"]
        return ticket_info["issue_name"].lower() in possible_ticket_issues

    def change_status_to_in_progress(self) -> None:
        ticket_info: Optional[Dict[str, str]] = self.get_ticket_info()
        is_valid_issue_type: bool = self.has_ticket_issue(ticket_info)
        if ticket_info:
            url: str = (
                f"https://{self.jira_domain}/rest/api/2/issue/{self.ticket_id}/transitions"
            )
            if is_valid_issue_type:
                if "backlog" in ticket_info["status"]:
                    transition_id: str = "121"  # Example ID for 'In Progress'
                    data: Dict = {"transition": {"id": transition_id}}
                    self.post(url, data)
                elif "selected" in ticket_info["status"]:
                    transition_id: str = "21"  # Example ID for 'In Progress'
                    data: Dict = {"transition": {"id": transition_id}}
                    self.post(url, data)

    def change_status_to_needs_review(self) -> None:
        ticket_info: Optional[Dict[str, str]] = self.get_ticket_info()
        is_valid_issue_type: bool = self.has_ticket_issue(ticket_info)
        if ticket_info:
            url: str = (
                f"https://{self.jira_domain}/rest/api/2/issue/{self.ticket_id}/transitions"
            )
            if is_valid_issue_type:
                if "progress" in ticket_info["status"]:
                    transition_id: str = "151"  # Example ID for 'Needs Code Review'
                    data: Dict = {"transition": {"id": transition_id}}
                    self.post(url, data)

    def get_jira_ticket_id(self, ticket_id: str) -> Optional[str]:
        match: Optional[re.Match] = re.search(r"vida[-_]?(\d{3,4})", ticket_id)
        if match:
            vida_number: str = match.group(1)
            vida_branch_name: str = f"vida-{vida_number}"
            print(f"Adding jira ticket for {vida_branch_name}")
            return vida_branch_name
        return None


if __name__ == "__main__":
    pr_title = os.environ["PR_TITLE"]
    jira_token = os.environ["JIRA_TOKEN"]

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--branch-name",
        dest="branch_name",
        type=str,
        help="provide branch name",
    )
    parser.add_argument(
        "--move-to-review",
        dest="should_review",
        type=bool,
        help="Move ticket to review",
    )
    parser.add_argument(
        "--move-to-in-progress",
        dest="move_to_in_progress",
        type=bool,
        help="Move ticket to in progress",
    )
    args = parser.parse_args()

    branch_name = args.branch_name

    should_review = args.should_review

    move_to_in_progress = args.move_to_in_progress

    instance = UpdateJira(jira_token, branch_name)
    if should_review:
        instance.change_status_to_needs_review()
    elif move_to_in_progress:
        instance.change_status_to_in_progress()
