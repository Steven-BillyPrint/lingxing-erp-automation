# ERP 自动化仓库操作约束

## 正式发布与部署

- 只有用户在当前任务中明确审核通过并授权发布/部署后，才能执行正式操作。
- 所有源码先通过分支、测试和 PR 合并到 `main`；禁止从功能分支直接部署。
- Windows 客户端发布统一执行：

  ```powershell
  .\scripts\publish_client_release.ps1 -ConfirmProductionRelease
  ```

- 客户端 Release 发布成功后，服务器部署统一执行：

  ```powershell
  .\scripts\deploy_production.ps1 -ConfirmProductionDeployment
  ```

- 共享部署私钥固定保存在
  `Z:\同事个人\颜奕超\ERP自动化部署专用\codex-production-deploy-ed25519`，主机指纹保存在同目录的
  `known_hosts`。任务可以调用这些文件，但不得读取、打印、复制、提交或上传私钥内容。
- 该共享密钥在服务器端必须绑定强制命令，只能部署已合并的 `main`；不得把管理员 Shell
  私钥当作常规发布入口。
- 发布顺序固定为：完整 CI → 发布 GitHub Release → 确认无活动任务 → 部署服务器 →
  健康检查。
- 本机客户端更新必须由用户打开现有 EXE，并使用程序内置的“检查更新”下载安装。除非用户在
  当前任务中另行明确授权，否则 Codex 不得直接替换项目 `dist`、任何客户端 EXE、正式安装
  目录或桌面快捷方式。
- `VERSION.txt` 只用于描述当前项目 EXE 的实际内置版本。只有用户明确要求，且已验证
  EXE 内置版本后，才可以单独修正该文件；不得借此绕过程序内置更新流程。
- `.codex-tmp/`、`output/`、`tmp/`、运行数据库、日志和用户文件不是源码，不得加入提交。
