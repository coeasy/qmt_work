import { useState } from "react";
import {
  Badge,
  Button,
  ConfirmModal,
  Input,
  Panel,
  Spinner,
  Tabs,
} from "@/design/primitives";
import { systemApi } from "@/services/api";
import { useAsync } from "@/hooks/useAsync";
import s from "../domain.module.css";

/**
 * 远程访问三档管理页面。
 *
 * 档位说明：
 * - off: 严格单机（仅本机可访问，loopback 免鉴权）
 * - lan: 内网多设备（局域网可访问，强制强密钥 + scope 分级）
 * - wan: 公网远程（lan + 强制 TOTP + signal.mode 默认 paper）
 *
 * 设计要点：
 * 1. 所有变更只写配置文件，需重启客户端生效
 * 2. API Key / TOTP Secret 仅在创建时返回一次，之后不可读取明文
 * 3. wan 档自动将 signal.mode 设为 paper（远程实盘风险过高）
 */
export function RemoteAccess() {
  const [tab, setTab] = useState<"mode" | "key" | "totp">("mode");

  const status = useAsync(() => systemApi.remoteAccessStatus(), []);
  const modes = useAsync(() => systemApi.remoteAccessModes(), []);

  const [busy, setBusy] = useState(false);
  const [banner, setBanner] = useState<{ tone: "ok" | "error"; text: string } | null>(null);
  const [confirm, setConfirm] = useState<{
    title: string;
    message: string;
    warn?: string;
    run: () => Promise<void>;
  } | null>(null);

  // TOTP 相关状态
  const [totpSecret, setTotpSecret] = useState("");
  const [totpUrl, setTotpUrl] = useState("");
  const [totpCode, setTotpCode] = useState("");
  const [totpResult, setTotpResult] = useState<string | null>(null);

  const data = status.data;

  const handleSetMode = (mode: string) => {
    const target = modes.data?.find((m) => m.mode === mode);
    if (!target) return;

    const doSet = async () => {
      setBusy(true);
      setBanner(null);
      try {
        const res = await systemApi.setRemoteAccessMode(mode);
        if (res.requires_restart) {
          setBanner({ tone: "ok", text: res.message });
        } else {
          setBanner({ tone: "ok", text: "档位未变化" });
        }
        if (res.hints.length > 0) {
          setBanner({ tone: "ok", text: res.hints.join("; ") });
        }
        status.reload();
      } catch (e: any) {
        setBanner({ tone: "error", text: e.message || "切换档位失败" });
      } finally {
        setBusy(false);
      }
    };

    setConfirm({
      title: `切换到 ${target.label}`,
      message: `确认切换到「${target.label}」？${target.description}`,
      warn: target.mode === "wan" ? "wan 档将强制 TOTP 二次确认，signal.mode 自动设为 paper" : undefined,
      run: doSet,
    });
  };

  const handleResetKey = async () => {
    const doReset = async () => {
      setBusy(true);
      setBanner(null);
      try {
        const res = await systemApi.resetRemoteAccessApiKey();
        setBanner({ tone: "ok", text: res.message });
        // 显示新密钥（仅此一次）
        if (res.api_key) {
          setConfirm({
            title: "新 API Key 已生成",
            message: `请妥善保存以下密钥（仅此一次显示）：\n\n${res.api_key}`,
            warn: res.warning,
            run: async () => {
              setConfirm(null);
              status.reload();
            },
          });
        }
      } catch (e: any) {
        setBanner({ tone: "error", text: e.message || "重置 API Key 失败" });
      } finally {
        setBusy(false);
      }
    };

    setConfirm({
      title: "重置主 API Key",
      message: "确认重置主 API Key？旧密钥在重启前仍可用，重启后必须使用新密钥。",
      warn: "新密钥将仅显示一次，请妥善保存",
      run: doReset,
    });
  };

  const handleEnableTotp = async () => {
    setBusy(true);
    setBanner(null);
    try {
      const res = await systemApi.enableRemoteAccessTotp();
      setTotpSecret(res.secret);
      setTotpUrl(res.otpauth_url);
      setBanner({ tone: "ok", text: res.message });
      status.reload();
    } catch (e: any) {
      setBanner({ tone: "error", text: e.message || "启用 TOTP 失败" });
    } finally {
      setBusy(false);
    }
  };

  const handleVerifyTotp = async () => {
    if (!totpCode) return;
    setBusy(true);
    setTotpResult(null);
    try {
      const res = await systemApi.verifyRemoteAccessTotp(totpCode);
      setTotpResult(res.verified ? "✓ TOTP 校验通过" : "✗ " + res.message);
    } catch (e: any) {
      setTotpResult("✗ " + (e.message || "校验失败"));
    } finally {
      setBusy(false);
    }
  };

  if (status.loading) {
    return (
      <div className={s.loading}>
        <Spinner /> 加载远程访问状态...
      </div>
    );
  }

  if (status.error || !data) {
    return (
      <Panel>
        <div className={s.error}>加载失败：{status.error || "未知错误"}</div>
        <Button onClick={() => status.reload()}>重试</Button>
      </Panel>
    );
  }

  return (
    <div className={s.page}>
      {/* 顶部状态概览 */}
      <Panel className={s.statusPanel}>
        <div className={s.statusRow}>
          <div>
            <span className={s.label}>当前档位</span>
            <Badge tone="info">{data.label}</Badge>
          </div>
          <div>
            <span className={s.label}>监听地址</span>
            <span className={s.value}>{data.host}:{data.port}</span>
          </div>
          <div>
            <span className={s.label}>API Key</span>
            {data.api_key.is_default ? (
              <Badge tone="warning">默认密钥（不安全）</Badge>
            ) : (
              <Badge tone="success">{data.api_key.prefix}</Badge>
            )}
          </div>
          <div>
            <span className={s.label}>TOTP</span>
            {data.totp.enabled ? (
              <Badge tone="success">已启用</Badge>
            ) : (
              <Badge tone="neutral">未启用</Badge>
            )}
          </div>
          <div>
            <span className={s.label}>信号模式</span>
            <Badge tone={data.signal.mode === "live" ? "warning" : "info"}>
              {data.signal.mode || "未设置"}
            </Badge>
          </div>
        </div>

        {/* 警告信息 */}
        {data.warnings.length > 0 && (
          <div className={s.warnings}>
            {data.warnings.map((w, i) => (
              <div key={i} className={s.warningItem}>⚠ {w}</div>
            ))}
          </div>
        )}
      </Panel>

      {/* 操作提示 */}
      {banner && (
        <div className={`${s.banner} ${banner.tone === "ok" ? s.bannerOk : s.bannerErr}`}>
          {banner.text}
        </div>
      )}

      {/* 主体内容 */}
      <Tabs
        items={[
          { key: "mode", label: "访问档位" },
          { key: "key", label: "API Key" },
          { key: "totp", label: "TOTP 二次确认" },
        ]}
        value={tab}
        onChange={(k) => setTab(k)}
      />

      {/* 档位管理 */}
      {tab === "mode" && (
        <Panel>
          <p className={s.hint}>
            切换档位后需重启客户端生效。wan 档将强制 TOTP 二次确认，signal.mode 自动设为 paper。
          </p>
          <div className={s.modeList}>
            {modes.data?.map((m) => (
              <div key={m.mode} className={s.modeItem}>
                <div className={s.modeHeader}>
                  <strong>{m.label}</strong>
                  <Badge tone={data.mode === m.mode ? "info" : "neutral"}>
                    {data.mode === m.mode ? "当前" : m.mode}
                  </Badge>
                </div>
                <p className={s.modeDesc}>{m.description}</p>
                <div className={s.modeBadges}>
                  {m.force_api_key && <Badge tone="neutral">强制强密钥</Badge>}
                  {m.force_totp && <Badge tone="warning">强制 TOTP</Badge>}
                  {m.signal_default !== "live" && (
                    <Badge tone="info">signal 默认 {m.signal_default}</Badge>
                  )}
                </div>
                {data.mode !== m.mode && (
                  <Button
                    size="sm"
                    disabled={busy}
                    onClick={() => handleSetMode(m.mode)}
                  >
                    切换到 {m.label}
                  </Button>
                )}
              </div>
            ))}
          </div>
        </Panel>
      )}

      {/* API Key 管理 */}
      {tab === "key" && (
        <Panel>
          <div className={s.keyInfo}>
            <div>
              <span className={s.label}>当前状态</span>
              {data.api_key.is_default ? (
                <Badge tone="warning">默认密钥 qmt-dev-key</Badge>
              ) : (
                <Badge tone="success">已设置强密钥</Badge>
              )}
            </div>
            <div>
              <span className={s.label}>密钥前缀</span>
              <span className={s.value}>{data.api_key.prefix}</span>
            </div>
            <div>
              <span className={s.label}>密钥长度</span>
              <span className={s.value}>{data.api_key.length} 字符</span>
            </div>
          </div>

          <p className={s.hint}>
            主 API Key 用于远程访问鉴权。重置后旧密钥在重启前仍可用，重启后必须使用新密钥。
            新密钥仅显示一次，请妥善保存。
          </p>

          <Button
            size="md"
            variant="danger"
            disabled={busy}
            onClick={handleResetKey}
          >
            重置 API Key
          </Button>

          <p className={s.hint}>
            配置文件位置：<code>{data.config_file}</code>
          </p>
        </Panel>
      )}

      {/* TOTP 管理 */}
      {tab === "totp" && (
        <Panel>
          <div className={s.keyInfo}>
            <div>
              <span className={s.label}>当前状态</span>
              {data.totp.enabled ? (
                <Badge tone="success">已启用</Badge>
              ) : (
                <Badge tone="neutral">未启用</Badge>
              )}
            </div>
            {data.totp.enabled && (
              <div>
                <span className={s.label}>剩余时间</span>
                <span className={s.value}>{data.totp.code_remaining_seconds}s</span>
              </div>
            )}
          </div>

          {!data.totp.enabled ? (
            <>
              <p className={s.hint}>
                启用 TOTP 二次确认后，wan 档远程交易将要求 6 位动态验证码。
                请使用 Authenticator App（如 Google Authenticator、Authy）扫描或导入 Secret。
              </p>
              <Button
                size="md"
                disabled={busy}
                onClick={handleEnableTotp}
              >
                启用 TOTP
              </Button>

              {totpSecret && (
                <div className={s.totpSecret}>
                  <p className={s.hint}>请立即保存以下 Secret（仅此一次显示）：</p>
                  <Input
                    value={totpSecret}
                    readOnly
                    onSelect={() => document.execCommand("copy")}
                  />
                  {totpUrl && (
                    <p className={s.hint}>
                      或导入 OTP URL：<code>{totpUrl}</code>
                    </p>
                  )}
                </div>
              )}
            </>
          ) : (
            <>
              <p className={s.hint}>TOTP 已启用。输入当前验证码验证配置是否正确：</p>
              <div className={s.totpVerify}>
                <Input
                  value={totpCode}
                  onChange={(e) => setTotpCode(e.target.value)}
                  placeholder="6 位验证码"
                  maxLength={6}
                  disabled={busy}
                />
                <Button
                  disabled={busy || !totpCode}
                  onClick={handleVerifyTotp}
                >
                  验证
                </Button>
              </div>
              {totpResult && (
                <div className={s.totpResult}>{totpResult}</div>
              )}
            </>
          )}
        </Panel>
      )}

      {/* 确认弹窗 */}
      {confirm && (
        <ConfirmModal
          open
          title={confirm.title}
          message={confirm.message}
          warn={confirm.warn}
          onConfirm={confirm.run}
          onCancel={() => setConfirm(null)}
          loading={busy}
        />
      )}
    </div>
  );
}

export default RemoteAccess;
