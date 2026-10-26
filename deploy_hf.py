# -*- coding: utf-8 -*-
"""一键部署到 Hugging Face Spaces（通过 hf-mirror 国内镜像）"""
import os
import sys
from huggingface_hub import HfApi

HF_ENDPOINT = "https://hf-mirror.com"
LOCAL_DIR = os.path.dirname(os.path.abspath(__file__))
SPACE_NAME = "quantai-platform"
SPACE_TITLE = "QuantAI 智能量化平台"

def main():
    token = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("HF_TOKEN")
    if not token:
        print("用法: python deploy_hf.py <HF_TOKEN>")
        print("或设置环境变量 HF_TOKEN")
        sys.exit(1)

    api = HfApi(endpoint=HF_ENDPOINT, token=token)
    username = api.whoami()["name"]
    repo_id = f"{username}/{SPACE_NAME}"

    print(f"[1/4] 创建/获取 Space: {repo_id}")
    api.create_repo(
        repo_id=repo_id,
        repo_type="space",
        space_sdk="docker",
        space_hardware="cpu-basic",
        private=False,
        exist_ok=True,
    )

    print(f"[2/4] 上传项目文件到 hf-mirror ...")
    api.upload_folder(
        folder_path=LOCAL_DIR,
        repo_id=repo_id,
        repo_type="space",
        ignore_patterns=[
            "data/cache.db", "__pycache__", "*.pyc", "deploy", "*.log", ".git",
        ],
    )

    print(f"[3/4] 触发构建（首次约 3-5 分钟）...")
    # 上传后 HF 会自动构建，这里只打印地址
    space_url = f"https://{username}-{SPACE_NAME}.hf.space"
    repo_url = f"https://hf-mirror.com/spaces/{repo_id}"

    print(f"[4/4] 部署完成！")
    print(f"  Space 仓库: {repo_url}")
    print(f"  访问地址:   {space_url}")
    print()
    print("提示: 首次构建需等待 3-5 分钟，可在仓库页面查看构建日志。")
    print("      构建成功后直接访问上面的 访问地址 即可，无需密码。")

if __name__ == "__main__":
    main()
