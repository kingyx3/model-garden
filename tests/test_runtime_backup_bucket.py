"""Offline guardrails for the optional client runtime backup storage boundary (#230).

Terraform format/validate and security checks remain the authoritative HCL validation.
"""
from __future__ import annotations

import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
BUCKET = ROOT / "infra" / "docker-host" / "gcp" / "runtime_backups.tf"


class RuntimeBackupBucketContractTests(unittest.TestCase):
    def test_client_runtime_backup_is_opt_in_and_project_scoped(self):
        source = BUCKET.read_text(encoding="utf-8")
        self.assertIn('variable "runtime_backup_enabled"', source)
        self.assertIn("default     = false", source)
        self.assertIn("var.runtime_backup_enabled ? 1 : 0", source)
        self.assertIn('substr(md5(var.project_id), 0, 10)', source)
        self.assertIn('local.name', source)

    def test_private_retained_objects_have_scoped_create_only_writer(self):
        source = BUCKET.read_text(encoding="utf-8")
        for declaration in (
            'uniform_bucket_level_access = true',
            'public_access_prevention    = "enforced"',
            'force_destroy               = false',
            "retention_policy {",
            "runtime_backup_retention_days * 86400",
            'resource "google_service_account" "backup_writer"',
            'resource "google_storage_bucket_iam_member" "backup_writer_create"',
            'role   = "roles/storage.objectCreator"',
            'member = "serviceAccount:${google_service_account.backup_writer[0].email}"',
            'resource "google_service_account_iam_member" "runtime_impersonates_backup_writer"',
            'role               = "roles/iam.serviceAccountTokenCreator"',
            'member             = "serviceAccount:${var.runtime_service_account}"',
            'resource "google_service_account" "backup_reader"',
            'resource "google_storage_bucket_iam_member" "backup_reader_view"',
            'role   = "roles/storage.objectViewer"',
            'member = "serviceAccount:${google_service_account.backup_reader[0].email}"',
        ):
            with self.subTest(declaration=declaration):
                self.assertIn(declaration, source)
        bucket_writer = source.split('resource "google_storage_bucket_iam_member" "backup_writer_create" {', 1)[1].split("\n}", 1)[0]
        self.assertNotIn("var.runtime_service_account", bucket_writer, "runtime must not have direct bucket permission")
        self.assertNotIn("roles/storage.objectAdmin", source)
        self.assertNotIn("roles/storage.admin", source)
        reader = source.split('resource "google_storage_bucket_iam_member" "backup_reader_view" {', 1)[1].split("\n}", 1)[0]
        self.assertNotIn("var.runtime_service_account", reader)
        self.assertIn("google_storage_bucket.runtime_backups[0].name", reader)
        self.assertNotIn('resource "google_service_account_iam_member" "runtime_impersonates_backup_reader"', source)
        self.assertIn('output "runtime_backup_reader_service_account"', source)


if __name__ == "__main__":
    unittest.main()
