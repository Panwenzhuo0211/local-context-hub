#!/bin/zsh
app_dir="${0:A:h}"
result=$("$app_dir/hub" report)
if [[ $? -eq 0 ]]; then
    report_path=$(print -r -- "$result" | /Library/Frameworks/Python.framework/Versions/3.14/bin/python3 -c 'import json,sys; print(json.load(sys.stdin)["report"])')
    /usr/bin/open "$report_path"
else
    print -r -- "$result"
    read '?按回车关闭。'
fi
