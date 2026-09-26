mkdir -p /data/soffy/tmp/test-repo
cd /data/soffy/tmp/test-repo
git init -q -b main .
echo "VEYA_READ_OK" > base.txt
git add base.txt
git commit -m "init"
opencode run --dir . --agent build "Read base.txt and report its contents." --format json
