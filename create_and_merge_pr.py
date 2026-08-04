import os

import requests

# 设置 GITHUB_TOKEN 环境变量
os.environ["GITHUB_TOKEN"] = "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"


def create_pull_request(owner, repo, title, body, base, head):
    url = f"https://api.github.com/repos/{owner}/{repo}/pulls"
    headers = {
        "Authorization": f"token {os.environ['GITHUB_TOKEN']}",
        "Accept": "application/vnd.github.v3+json",
    }
    data = {"title": title, "body": body, "base": base, "head": head}
    response = requests.post(url, json=data, headers=headers)
    if response.status_code == 201:
        print("Pull request created successfully")
        return response.json()["number"]
    else:
        print(f"Failed to create pull request: {response.status_code} {response.text}")
        return None


def merge_pull_request(owner, repo, pr_number):
    url = f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}/merge"
    headers = {
        "Authorization": f"token {os.environ['GITHUB_TOKEN']}",
        "Accept": "application/vnd.github.v3+json",
    }
    data = {"commit_message": "Merge pull request"}
    response = requests.put(url, json=data, headers=headers)
    if response.status_code == 200:
        print("Pull request merged successfully")
    else:
        print(f"Failed to merge pull request: {response.status_code} {response.text}")


# 创建 Pull Request
pr_number = create_pull_request(
    "soffy88",
    "example-repo",
    "Add new line to README",
    "This is a test PR",
    "main",
    "feature-branch",
)

# 合并 Pull Request
if pr_number is not None:
    merge_pull_request("soffy88", "example-repo", pr_number)
