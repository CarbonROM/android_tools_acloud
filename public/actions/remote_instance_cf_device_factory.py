# Copyright 2019 - The Android Open Source Project
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""RemoteInstanceDeviceFactory provides basic interface to create a cuttlefish
device factory."""

import logging

from acloud.internal import constants
from acloud.internal.lib import cvd_utils
from acloud.internal.lib import utils
from acloud.public.actions import gce_device_factory
from acloud.public import report
from acloud.pull import pull


logger = logging.getLogger(__name__)
_SCREEN_CONSOLE_COMMAND = "screen ~/cuttlefish_runtime/console"


class RemoteInstanceDeviceFactory(gce_device_factory.GCEDeviceFactory):
    """A class that can produce a cuttlefish device.

    Attributes:
        avd_spec: AVDSpec object that tells us what we're going to create.
        cfg: An AcloudConfig instance.
        local_image_artifact: A string, path to local image.
        cvd_host_package_artifact: A string, path to cvd host package.
        report_internal_ip: Boolean, True for the internal ip is used when
                            connecting from another GCE instance.
        credentials: An oauth2client.OAuth2Credentials instance.
        compute_client: An object of cvd_compute_client.CvdComputeClient.
        ssh: An Ssh object.
    """
    def __init__(self, avd_spec, local_image_artifact=None,
                 cvd_host_package_artifact=None):
        super().__init__(avd_spec, local_image_artifact)
        self._all_logs = {}
        self._cvd_host_package_artifact = cvd_host_package_artifact

    # pylint: disable=broad-except
    def CreateInstance(self):
        """Create a single configured cuttlefish device.

        Returns:
            A string, representing instance name.
        """
        instance = self._CreateGceInstance()
        # If instance is failed, no need to go next step.
        if instance in self.GetFailures():
            return instance
        try:
            image_args = self._ProcessArtifacts(self._avd_spec.image_source)
            failures = self._compute_client.LaunchCvd(
                instance,
                self._avd_spec,
                self._cfg.extra_data_disk_size_gb,
                boot_timeout_secs=self._avd_spec.boot_timeout_secs,
                extra_args=image_args)
            for failing_instance, error_msg in failures.items():
                self._SetFailures(failing_instance, error_msg)
        except Exception as e:
            self._SetFailures(instance, e)

        self._FindLogFiles(
            instance,
            instance in self.GetFailures() and not self._avd_spec.no_pull_log)
        return instance

    def _ProcessArtifacts(self, image_source):
        """Process artifacts.

        - If images source is local, tool will upload images from local site to
          remote instance.
        - If images source is remote, tool will download images from android
          build to remote instance. Before download images, we have to update
          fetch_cvd to remote instance.

        Args:
            image_source: String, the type of image source is remote or local.

        Returns:
            A list of strings, the launch_cvd arguments.
        """
        if image_source == constants.IMAGE_SRC_LOCAL:
            self._UploadLocalImageArtifacts(self._local_image_artifact,
                                            self._cvd_host_package_artifact,
                                            self._avd_spec.local_image_dir)
        elif image_source == constants.IMAGE_SRC_REMOTE:
            self._compute_client.UpdateFetchCvd()
            self._FetchBuild(self._avd_spec)

        if self._avd_spec.mkcert and self._avd_spec.connect_webrtc:
            self._compute_client.UpdateCertificate()

        if self._avd_spec.extra_files:
            self._compute_client.UploadExtraFiles(self._avd_spec.extra_files)

        return cvd_utils.UploadExtraImages(self._ssh, self._avd_spec)

    def _FetchBuild(self, avd_spec):
        """Download CF artifacts from android build.

        Args:
            avd_spec: AVDSpec object that tells us what we're going to create.
        """
        self._compute_client.FetchBuild(
            avd_spec.remote_image[constants.BUILD_ID],
            avd_spec.remote_image[constants.BUILD_BRANCH],
            avd_spec.remote_image[constants.BUILD_TARGET],
            avd_spec.system_build_info[constants.BUILD_ID],
            avd_spec.system_build_info[constants.BUILD_BRANCH],
            avd_spec.system_build_info[constants.BUILD_TARGET],
            avd_spec.kernel_build_info[constants.BUILD_ID],
            avd_spec.kernel_build_info[constants.BUILD_BRANCH],
            avd_spec.kernel_build_info[constants.BUILD_TARGET],
            avd_spec.bootloader_build_info[constants.BUILD_ID],
            avd_spec.bootloader_build_info[constants.BUILD_BRANCH],
            avd_spec.bootloader_build_info[constants.BUILD_TARGET],
            avd_spec.ota_build_info[constants.BUILD_ID],
            avd_spec.ota_build_info[constants.BUILD_BRANCH],
            avd_spec.ota_build_info[constants.BUILD_TARGET])

    @utils.TimeExecute(function_description="Processing and uploading local images")
    def _UploadLocalImageArtifacts(self,
                                   local_image_zip,
                                   cvd_host_package_artifact,
                                   images_dir):
        """Upload local images and avd local host package to instance.

        Args:
            local_image_zip: String, path to zip of local images which
                             build from 'm dist'.
            cvd_host_package_artifact: String, path to cvd host package.
            images_dir: String, directory of local images which build
                        from 'm'.
        """
        if local_image_zip:
            cvd_utils.UploadImageZip(self._ssh, local_image_zip)
        else:
            cvd_utils.UploadImageDir(self._ssh, images_dir)
        cvd_utils.UploadCvdHostPackage(self._ssh, cvd_host_package_artifact)

    def _FindLogFiles(self, instance, download):
        """Find and pull all log files from instance.

        Args:
            instance: String, instance name.
            download: Whether to download the files to a temporary directory
                      and show messages to the user.
        """
        log_files = pull.GetAllLogFilePaths(self._ssh)
        self._all_logs[instance] = [
            report.LogFile("/var/log/kern.log", constants.LOG_TYPE_KERNEL_LOG,
                           "host_kernel.log")]
        self._all_logs[instance].extend(cvd_utils.ConvertRemoteLogs(log_files))
        if download:
            error_log_folder = pull.PullLogs(self._ssh, log_files, instance)
            self._compute_client.ExtendReportData(constants.ERROR_LOG_FOLDER,
                                                  error_log_folder)

    def GetOpenWrtInfoDict(self):
        """Get openwrt info dictionary.

        Returns:
            A openwrt info dictionary. None for the case is not openwrt device.
        """
        if not self._avd_spec.openwrt:
            return None
        return {"ssh_command": self._compute_client.GetSshConnectCmd(),
                "screen_command": _SCREEN_CONSOLE_COMMAND}

    def GetBuildInfoDict(self):
        """Get build info dictionary.

        Returns:
            A build info dictionary. None for local image case.
        """
        if self._avd_spec.image_source == constants.IMAGE_SRC_LOCAL:
            return None
        return cvd_utils.GetRemoteBuildInfoDict(self._avd_spec)

    def GetLogs(self):
        """Get all device logs.

        Returns:
            A dictionary that maps instance names to lists of report.LogFile.
        """
        return self._all_logs
