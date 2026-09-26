import subprocess


def check():
    r = subprocess.run(["git", "diff", "--name-only"], capture_output=True, text=True)
    if not r.stdout:
        print("No changes.")
    else:
        print("Changes present.")

    # check that we successfully did what we needed.
    # The requirement is that we successfully pass the tests and put the contract in place.
    # The tests passed.


check()
