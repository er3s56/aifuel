// SPDX-License-Identifier: GPL-3.0-only
#pragma once

constexpr auto kWidth = L"aifuel.TaskbarWidth.v1";
constexpr auto kLease = L"aifuel.TaskbarLease.v1";
constexpr auto kReady = L"aifuel.TaskbarReady.v1";

inline bool HasLiveLease(HWND window) {
    const auto lease = reinterpret_cast<ULONG_PTR>(GetPropW(window, kLease));
    const auto width = reinterpret_cast<ULONG_PTR>(GetPropW(window, kWidth));
    return lease && GetTickCount64() - lease <= 2000 && width >= 64 && width <= 1600;
}

inline HWND FindQuotaWindow(HWND current) {
    // Qt popups can inherit the title "aifuel". A title search can select the
    // context menu instead of the quota panel and accidentally release its space.
    if (HasLiveLease(current)) return current;
    HWND result{};
    EnumWindows([](HWND window, LPARAM parameter) -> BOOL {
        if (!HasLiveLease(window)) return TRUE;
        *reinterpret_cast<HWND*>(parameter) = window;
        return FALSE;
    }, reinterpret_cast<LPARAM>(&result));
    return result;
}
