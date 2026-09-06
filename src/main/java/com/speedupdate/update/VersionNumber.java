package com.speedupdate.update;

/**
 * 整合包版本号工具。
 *
 * 版本号格式：主版本.次版本.补丁版本，每段三位数字（不足补零），
 * 如 1.0.000、2.1.230、10.5.123。
 *
 * 解析失败或格式非法时按 0.0.000 处理（视为最旧版本），
 * 保证异常数据不会导致更新流程崩溃。
 */
public final class VersionNumber implements Comparable<VersionNumber> {
    private final long major;
    private final long minor;
    private final long patch;

    private VersionNumber(long major, long minor, long patch) {
        this.major = major;
        this.minor = minor;
        this.patch = patch;
    }

    public static VersionNumber parse(String text) {
        if (text == null) {
            return new VersionNumber(0, 0, 0);
        }
        String[] parts = text.trim().split("\\.");
        if (parts.length < 3) {
            return new VersionNumber(0, 0, 0);
        }
        try {
            long major = Long.parseLong(parts[0].trim());
            long minor = Long.parseLong(parts[1].trim());
            long patch = Long.parseLong(parts[2].trim());
            return new VersionNumber(major, minor, patch);
        } catch (NumberFormatException e) {
            return new VersionNumber(0, 0, 0);
        }
    }

    /** 该版本是否比 other 新（严格大于）。 */
    public boolean isNewerThan(String other) {
        return compareTo(parse(other)) > 0;
    }

    @Override
    public int compareTo(VersionNumber o) {
        if (major != o.major) {
            return Long.compare(major, o.major);
        }
        if (minor != o.minor) {
            return Long.compare(minor, o.minor);
        }
        return Long.compare(patch, o.patch);
    }

    @Override
    public String toString() {
        return major + "." + minor + "." + patch;
    }
}
