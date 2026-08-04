import os

import httpx


def create_pull_request_comment(repo, pr_number, comment):
    url = f"https://api.github.com/repos/{repo}/issues/{pr_number}/comments"
    github_token = os.environ.get("GITHUB_TOKEN", "")
    if not github_token:
        print("GITHUB_TOKEN not set; skipping comment")
        return None
    headers = {"Authorization": f"token {github_token}", "Accept": "application/vnd.github.v3+json"}
    data = {"body": comment}
    with httpx.Client() as client:
        response = client.post(url, json=data, headers=headers)
    if response.status_code == 201:
        print("Comment created successfully")
    else:
        print(f"Failed to create comment: {response.status_code} {response.text}")
