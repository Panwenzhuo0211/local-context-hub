#!/usr/bin/env python3
"""Interactive, local owner setup. Never prints or uploads credentials."""
import json
import os
from context_hub.agents import Registry,agent_id
from context_hub.cli import DEFAULT
from context_hub.matters import Matters


def main(root=DEFAULT,reveal=True):
    os.umask(0o077)
    print('Context Hub：接入新 Agent（本机设置，不会上传会话）')
    name=agent_id(input('Agent 英文标识，如 muse、claude：').strip())
    registry=Registry(root)
    if name in registry._load()['agents']:
        result=registry.bundle(name)
        print('该 Agent 已登记，保留原权限，仅重新生成连接说明。')
    else:
        m=Matters(root)
        try:
            matters=m.list()
            if not matters:
                matter=m.create(input('尚无共享事项，请为这个 Agent 创建一个事项名称：').strip(),actor='user')
                matters=[matter]
        finally:m.close()
        for i,matter in enumerate(matters,1):print(f'{i}. {matter["name"]}')
        raw=input('允许访问哪些事项？填写序号，用逗号分隔：')
        indexes=[int(x.strip())-1 for x in raw.split(',')]
        if not indexes or any(x<0 or x>=len(matters) for x in indexes):raise ValueError('事项序号无效')
        ids=[matters[x]['id'] for x in indexes]
        write=input('允许署名保存观点、任务与摘要？[y/N]：').strip().lower()=='y'
        result=registry.enroll(name,ids,write)
    if input('为已有授权的本机写文件工具启用文件通道？[y/N]：').strip().lower()=='y':
        from context_hub.file_bridge import enable
        channel=enable(root,name)
        print('文件通道说明：'+channel['instructions'])
        print('需保持 context-hub-file-bridge 后台运行；只有读取能力的客户端不能用文件通道。')
    print('本机登记完成；外部客户端仍需完成一次连接。')
    print('连接说明目录：'+result['connection_directory'])
    if reveal and os.name=='posix' and __import__('sys').platform=='darwin':
        __import__('subprocess').run(['open',result['connection_directory']],check=False)


if __name__=='__main__':
    try:main()
    except (ValueError,OSError,EOFError) as exc:print('未完成：'+str(exc))
