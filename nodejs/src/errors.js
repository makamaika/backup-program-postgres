// 共通例外定義。

export class PgBackupError extends Error {
  constructor(message) {
    super(message);
    this.name = this.constructor.name;
  }
}

export class ProfileError extends PgBackupError {}
export class ToolError extends PgBackupError {}
export class BackupError extends PgBackupError {}
export class RestoreError extends PgBackupError {}
export class AgentError extends PgBackupError {}
export class DataEditError extends PgBackupError {}
