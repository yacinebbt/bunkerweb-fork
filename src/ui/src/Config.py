from copy import deepcopy
from hashlib import sha256
from io import BytesIO
from flask import flash
from glob import iglob
from json import load as json_load
from os import listdir
from os.path import basename
from pathlib import Path
from re import search as re_search
from subprocess import run, DEVNULL, STDOUT
from tarfile import open as tar_open
from time import sleep
from typing import List, Tuple
from uuid import uuid4


class Config:
    def __init__(self, logger, db) -> None:
        with open("/usr/share/bunkerweb/settings.json", "r") as f:
            self.__settings: dict = json_load(f)

        self.__logger = logger
        self.__db = db

        if not Path("/usr/sbin/nginx").exists():
            while not self.__db.is_initialized():
                self.__logger.warning(
                    "Database is not initialized, retrying in 5s ...",
                )
                sleep(5)

            env = self.__db.get_config()
            while not self.__db.is_first_config_saved() or not env:
                self.__logger.warning(
                    "Database doesn't have any config saved yet, retrying in 5s ...",
                )
                sleep(5)
                env = self.__db.get_config()

            self.__logger.info("Database is ready")
            Path("/var/tmp/bunkerweb/ui.healthy").write_text("ok")

    def __env_to_dict(self, filename: str) -> dict:
        """Converts the content of an env file into a dict

        Parameters
        ----------
        filename : str
            the path to the file to convert to dict

        Returns
        -------
        dict
            The values of the file converted to dict
        """
        if not Path(filename).is_file():
            return {}

        data = {}
        for line in Path(filename).read_text().split("\n"):
            if not "=" in line:
                continue
            var = line.split("=")[0]
            val = line.replace(f"{var}=", "", 1)
            data[var] = val

        return data

    def __dict_to_env(self, filename: str, variables: dict) -> None:
        """Converts the content of a dict into an env file

        Parameters
        ----------
        filename : str
            The path to save the env file
        variables : dict
            The dict to convert to env file
        """
        Path(filename).write_text(
            "\n".join(f"{k}={variables[k]}" for k in sorted(variables))
        )

    def __gen_conf(self, global_conf: dict, services_conf: list[dict]) -> None:
        """Generates the nginx configuration file from the given configuration

        Parameters
        ----------
        variables : dict
            The configuration to add to the file

        Raises
        ------
        Exception
            If an error occurred during the generation of the configuration file, raises this exception
        """
        conf = deepcopy(global_conf)

        servers = []
        plugins_settings = self.get_plugins_settings()
        for service in services_conf:
            server_name = service["SERVER_NAME"].split(" ")[0]
            for k in service:
                key_without_server_name = k.replace(f"{server_name}_", "")
                if (
                    plugins_settings[key_without_server_name]["context"] != "global"
                    if key_without_server_name in plugins_settings
                    else True
                ):
                    if not k.startswith(server_name) or k in plugins_settings:
                        conf[f"{server_name}_{k}"] = service[k]
                    else:
                        conf[k] = service[k]

            servers.append(server_name)

        conf["SERVER_NAME"] = " ".join(servers)
        env_file = f"/tmp/{uuid4()}.env"
        self.__dict_to_env(env_file, conf)
        proc = run(
            [
                "python3",
                "/usr/share/bunkerweb/gen/save_config.py",
                "--variables",
                env_file,
                "--method",
                "ui",
            ],
            stdin=DEVNULL,
            stderr=STDOUT,
        )

        if proc.returncode != 0:
            raise Exception(f"Error from generator (return code = {proc.returncode})")

        Path(env_file).unlink()

    def get_plugins_settings(self) -> dict:
        return {
            **{k: v for x in self.get_plugins() for k, v in x["settings"].items()},
            **self.__settings,
        }

    def get_plugins(
        self, *, external: bool = False, with_data: bool = False
    ) -> List[dict]:
        if not Path("/usr/sbin/nginx").exists():
            plugins = self.__db.get_plugins(external=external, with_data=with_data)
            plugins.sort(key=lambda x: x["name"])

            if not external:
                general_plugin = None
                for x, plugin in enumerate(plugins):
                    if plugin["name"] == "General":
                        general_plugin = plugin
                        del plugins[x]
                        break
                plugins.insert(0, general_plugin)

            return plugins

        plugins = []

        for foldername in list(iglob("/etc/bunkerweb/plugins/*")) + (
            list(iglob("/usr/share/bunkerweb/core/*") if not external else [])
        ):
            content = listdir(foldername)
            if "plugin.json" not in content:
                continue

            with open(f"{foldername}/plugin.json", "r") as f:
                plugin = json_load(f)

            plugin.update(
                {
                    "page": False,
                    "external": foldername.startswith("/etc/bunkerweb/plugins"),
                }
            )

            plugin["method"] = "ui" if plugin["external"] else "manual"

            if "ui" in content:
                if "template.html" in listdir(f"{foldername}/ui"):
                    plugin["page"] = True

            if with_data:
                plugin_content = BytesIO()
                with tar_open(fileobj=plugin_content, mode="w:gz") as tar:
                    tar.add(
                        foldername,
                        arcname=basename(foldername),
                        recursive=True,
                    )
                plugin_content.seek(0)
                value = plugin_content.getvalue()

                plugin["data"] = value
                plugin["checksum"] = sha256(value).hexdigest()

            plugins.append(plugin)

        plugins.sort(key=lambda x: x["name"])

        with open("/usr/share/bunkerweb/settings.json", "r") as f:
            plugins.insert(
                0,
                {
                    "id": "general",
                    "order": 999,
                    "name": "General",
                    "description": "The general settings for the server",
                    "version": "0.1",
                    "external": False,
                    "method": "manual",
                    "page": False,
                    "settings": json_load(f),
                },
            )

        return plugins

    def get_settings(self) -> dict:
        return self.__settings

    def get_config(self, methods: bool = True) -> dict:
        """Get the nginx variables env file and returns it as a dict

        Returns
        -------
        dict
            The nginx variables env file as a dict
        """
        if Path("/usr/sbin/nginx").exists():
            return {
                k: ({"value": v, "method": "ui"} if methods else v)
                for k, v in self.__env_to_dict("/etc/nginx/variables.env").items()
            }

        return self.__db.get_config(methods=methods)

    def get_services(self, methods: bool = True) -> list[dict]:
        """Get nginx's services

        Returns
        -------
        list
            The services
        """
        if Path("/usr/sbin/nginx").exists():
            services = []
            plugins_settings = self.get_plugins_settings()
            for filename in iglob("/etc/nginx/**/variables.env"):
                service = filename.split("/")[3]
                env = {
                    k.replace(f"{service}_", ""): (
                        {"value": v, "method": "ui"} if methods else v
                    )
                    for k, v in self.__env_to_dict(filename).items()
                    if k.startswith(f"{service}_") or k in plugins_settings
                }
                services.append(env)

            return services

        return self.__db.get_services_settings(methods=methods)

    def check_variables(self, variables: dict, _global: bool = False) -> int:
        """Testify that the variables passed are valid

        Parameters
        ----------
        variables : dict
            The dict to check

        Returns
        -------
        int
            Return the error code
        """
        error = 0
        plugins_settings = self.get_plugins_settings()
        for k, v in variables.items():
            check = False

            if k in plugins_settings:
                if _global ^ (plugins_settings[k]["context"] == "global"):
                    error = 1
                    flash(f"Variable {k} is not valid.", "error")
                    continue

                setting = k
            else:
                setting = k[0 : k.rfind("_")]
                if (
                    setting not in plugins_settings
                    or "multiple" not in plugins_settings[setting]
                ):
                    error = 1
                    flash(f"Variable {k} is not valid.", "error")
                    continue

            if not (
                _global ^ (plugins_settings[setting]["context"] == "global")
            ) and re_search(plugins_settings[setting]["regex"], v):
                check = True

            if not check:
                error = 1
                flash(f"Variable {k} is not valid.", "error")
                continue

        return error

    def reload_config(self) -> None:
        self.__gen_conf(
            self.get_config(methods=False), self.get_services(methods=False)
        )

    def new_service(self, variables: dict, edit: bool = False) -> Tuple[str, int]:
        """Creates a new service from the given variables

        Parameters
        ----------
        variables : dict
            The settings for the new service

        Returns
        -------
        str
            The confirmation message

        Raises
        ------
        Exception
            raise this if the service already exists
        """
        services = self.get_services(methods=False)
        for i, service in enumerate(services):
            if service["SERVER_NAME"] == variables["SERVER_NAME"] or service[
                "SERVER_NAME"
            ] in variables["SERVER_NAME"].split(" "):
                if not edit:
                    return (
                        f"Service {service['SERVER_NAME'].split(' ')[0]} already exists.",
                        1,
                    )

                services.pop(i)

        services.append(variables)
        self.__gen_conf(self.get_config(methods=False), services)
        return (
            f"Configuration for {variables['SERVER_NAME'].split(' ')[0]} has been generated.",
            0,
        )

    def edit_service(self, old_server_name: str, variables: dict) -> Tuple[str, int]:
        """Edits a service

        Parameters
        ----------
        old_server_name : str
            The old server name
        variables : dict
            The settings to change for the service

        Returns
        -------
        str
            the confirmation message
        """
        message, error = self.delete_service(old_server_name)

        if error:
            return message, error

        message, error = self.new_service(variables, edit=True)

        if error:
            return message, error

        return (
            f"Configuration for {old_server_name.split(' ')[0]} has been edited.",
            error,
        )

    def edit_global_conf(self, variables: dict) -> str:
        """Edits the global conf

        Parameters
        ----------
        variables : dict
            The settings to change for the conf

        Returns
        -------
        str
            the confirmation message
        """
        self.__gen_conf(
            self.get_config(methods=False) | variables, self.get_services(methods=False)
        )
        return f"The global configuration has been edited."

    def delete_service(self, service_name: str) -> Tuple[str, int]:
        """Deletes a service

        Parameters
        ----------
        service_name : str
            The name of the service to edit

        Returns
        -------
        str
            The confirmation message

        Raises
        ------
        Exception
            raises this if the service_name given isn't found
        """
        service_name = service_name.split(" ")[0]
        full_env = self.get_config(methods=False)
        services = self.get_services(methods=False)
        new_services = []
        found = False

        for service in services:
            if service["SERVER_NAME"].split(" ")[0] == service_name:
                found = True
            else:
                new_services.append(service)

        if not found:
            return f"Can't delete missing {service_name} configuration.", 1

        full_env["SERVER_NAME"] = " ".join(
            [s for s in full_env["SERVER_NAME"].split(" ") if s != service_name]
        )

        new_env = deepcopy(full_env)

        for k in full_env:
            if k.startswith(service_name):
                new_env.pop(k)

                for service in new_services:
                    if k in service:
                        service.pop(k)

        self.__gen_conf(new_env, new_services)
        return f"Configuration for {service_name} has been deleted.", 0
