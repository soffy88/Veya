import subprocess

def git_init():
    subprocess.run(["git", "init"])

def git_add(file):
    subprocess.run(["git", "add", file])

def git_commit(message):
    subprocess.run(["git", "commit", "-m", message])

def git_push(remote, branch):
    subprocess.run(["git", "push", remote, branch])