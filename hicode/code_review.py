import requests

def create_pull_request_comment(repo, pr_number, comment):
    url = f"https://api.github.com/repos/{repo}/issues/{pr_number}/comments"
    headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json"
    }
    data = {
        "body": comment
    }
    response = requests.post(url, json=data, headers=headers)
    if response.status_code == 201:
        print("Comment created successfully")
    else:
        print(f"Failed to create comment: {response.status_code} {response.text}")