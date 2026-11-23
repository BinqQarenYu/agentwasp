# Google Project IDX Configuration for AgentWasp
# https://developers.google.com/idx/guides/customize-idx-env
{ pkgs, ... }: {
  channel = "stable-24.05";

  packages = [
    pkgs.python311
    pkgs.python311Packages.pip
    pkgs.python311Packages.virtualenv
    pkgs.docker
    pkgs.docker-compose
    pkgs.git
    pkgs.curl
    pkgs.jq
  ];

  env = {
    PORT = "8080";
    DASHBOARD_PORT = "8080";
    PYTHONUNBUFFERED = "1";
  };

  idx = {
    extensions = [
      "ms-python.python"
      "ms-azuretools.vscode-docker"
    ];

    previews = {
      enable = true;
      previews = {
        web = {
          command = ["docker", "compose", "up", "agent-core"];
          manager = "web";
          env = {
            PORT = "$PORT";
          };
        };
      };
    };

    workspace = {
      onCreate = {
        setup = ''
          cp -n .env.example .env || true
        '';
      };
      onStart = {
        start-containers = ''
          docker compose up -d postgres redis
        '';
      };
    };
  };
}
