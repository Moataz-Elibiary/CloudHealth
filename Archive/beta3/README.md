# CloudHealth Beta 3

CloudHealth Beta 3 splits the old monolithic flow into:

- a local bootstrapper
- a frontend orchestrator
- a bastion-side backend worker
- shared inventory and result models

The target model is:

1. Start `bootstrapper.py`.
2. Sync the local runtime from a version-source bastion.
3. Launch the frontend dashboard.
4. Push the backend worker to each bastion.
5. Open an SSH tunnel per bastion.
6. Stream diagnostic results back to the browser over WebSocket.

## Layout

```text
beta3/
  bootstrapper.py
  build.spec
  config.yaml
  inventory.xlsx
  logger.py
  frontend/
    app.py
    config_loader.py
    report_generator.py
    tunnel_manager.py
    ws_proxy.py
    static/
      index.html
  backend/
    main.py
    check_runner.py
    result.py
    ssh_client.py
    checks/
      ocp_checks.py
      cvim_checks.py
      host_checks.py
    core/
      __init__.py
      inventory.py
      models.py
      ssh.py
      crypto.py
```

## Run Locally

Bootstrap:

```powershell
python bootstrapper.py
```

Frontend only:

```powershell
python frontend\app.py
```

Backend worker only:

```powershell
python backend\main.py --port 8100
```

Lock and cleanup verification:

```powershell
python ..\verify_beta3.py
```

## Inventory

`inventory.xlsx` is the default workbook used by the frontend and backend inventory loader.

Supported cluster columns:

- `Cluster Name` or `cluster_name`
- `Type` or `type`
- `Installer IP` or `installer_host`
- `SSH User` or `ssh_username`
- `SSH Pass/Key` or `ssh_password`
- `SSH Key` or `ssh_private_key`
- `Enabled` or `enabled`
- `Node IPs` or `node_ips`

Supported optional node sheet columns:

- `Cluster Name` or `cluster_name`
- `Node IP`, `IP`, or `node_ip`
- `SSH User` or `ssh_username`
- `SSH Pass`, `Pass`, or `ssh_password`

## Notes

- The frontend dashboard uses inline JavaScript inside `frontend/static/index.html`.
- The backend worker creates a runtime lock file at `/tmp/cloud_health/hc.lock` on Linux, or the system temp directory on Windows.
- `bundle_vendor.py` can download backend dependencies into `backend/vendor` for offline bastion use.
