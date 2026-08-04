import os

import requests

# 设置 GITHUB_TOKEN 环境变量
os.environ["GITHUB_TOKEN"] = "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"


def create_pull_request_comment(repo, pr_number, comment):
    url = f"https://api.github.com/repos/{repo}/issues/{pr_number}/comments"
    headers = {
        "Authorization": f"token {os.environ['GITHUB_TOKEN']}",
        "Accept": "application/vnd.github.v3+json",
    }
    data = {"body": comment}
    response = requests.post(url, json=data, headers=headers)
    if response.status_code == 201:
        print("Comment created successfully")
    else:
        print(f"Failed to create comment: {response.status_code} {response.text}")


# 测试代码审查工具
create_pull_request_comment("soffy88/example-repo", 1, "This is a test comment")
