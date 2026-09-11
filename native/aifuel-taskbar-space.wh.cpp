// ==WindhawkMod==
// @id              aifuel-taskbar-space
// @name            aifuel taskbar space
// @description     Reserve space beside the system tray while aifuel is running
// @version         1.0.3
// @author          aifuel contributors
// @include         explorer.exe
// @architecture    amd64
// @compilerOptions -lole32 -loleaut32 -lruntimeobject
// ==/WindhawkMod==
// SPDX-License-Identifier: GPL-3.0-only
#include <windhawk_utils.h>
#include <atomic>
#include <chrono>
#include <thread>
#undef GetCurrentTime
#include <winrt/Windows.Foundation.h>
#include <winrt/Windows.UI.Xaml.h>
#include <winrt/Windows.UI.Xaml.Media.h>
using namespace winrt::Windows::UI::Xaml;
#include "windhawk_taskbar_helpers.h"
#include "taskbar_lease.h"

HWND taskbarWindow{}, quotaWindow{};
UINT_PTR timerId{};
std::atomic<bool> stopping{};
std::thread startupWorker;
winrt::weak_ref<FrameworkElement> savedIconArea;
winrt::Windows::Foundation::IInspectable savedWidth{nullptr}, savedAlignment{nullptr};
double appliedWidth = -1;

void Restore() {
    if (quotaWindow) RemoveProp(quotaWindow, kReady);
    if (auto frame = savedIconArea.get()) {
        auto restore = [&](DependencyProperty property, auto const& value) {
            if (value == DependencyProperty::UnsetValue()) frame.ClearValue(property);
            else frame.SetValue(property, value);
        };
        // Avoid overwriting a subsequent customization by another extension.
        if (frame.Width() == appliedWidth) {
            restore(FrameworkElement::WidthProperty(), savedWidth);
        }
        if (frame.HorizontalAlignment() == HorizontalAlignment::Left) {
            restore(FrameworkElement::HorizontalAlignmentProperty(), savedAlignment);
        }
    }
    savedIconArea = {};
    savedWidth = savedAlignment = nullptr;
    appliedWidth = -1;
    quotaWindow = nullptr;
}

FrameworkElement NamedChild(FrameworkElement parent, wchar_t const* name) {
    if (!parent) return nullptr;
    for (int i = 0, n = Media::VisualTreeHelper::GetChildrenCount(parent); i < n; ++i) {
        auto child = Media::VisualTreeHelper::GetChild(parent, i).try_as<FrameworkElement>();
        if (child && child.Name() == name) return child;
    }
    return nullptr;
}

void Tick() {
    if (!IsWindow(taskbarWindow)) {
        Restore();
        taskbarWindow = FindWindow(L"Shell_TrayWnd", nullptr);
        DWORD process{};
        GetWindowThreadProcessId(taskbarWindow, &process);
        if (process != GetCurrentProcessId()) { taskbarWindow = nullptr; return; }
    }
    HWND quota = FindQuotaWindow(quotaWindow);
    const auto width = reinterpret_cast<ULONG_PTR>(GetProp(quota, kWidth));
    if (!quota) {
        if (appliedWidth >= 0) Restore();
        return;
    }
    auto root = GetTaskbarXamlRoot(taskbarWindow);
    if (!root || !root.Content()) { Restore(); return; }
    auto content = root.Content().try_as<FrameworkElement>();
    if (!content) { Restore(); return; }
    FrameworkElement frame{nullptr};
    double trayWidth = 0;
    for (int i = 0, n = Media::VisualTreeHelper::GetChildrenCount(content); i < n; ++i) {
        auto child = Media::VisualTreeHelper::GetChild(content, i).try_as<FrameworkElement>();
        if (!child) continue;
        if (child.Name() == L"TaskbarFrame") frame = child;
        if (winrt::get_class_name(child) == L"SystemTray.SystemTrayFrame") trayWidth = child.ActualWidth();
    }
    if (!frame || trayWidth <= 0 || content.ActualWidth() < trayWidth + width + 240) {
        Restore(); return;
    }
    // TaskbarFrame also owns the full-width acrylic background. Constrain only
    // its button repeater, so the background remains intact behind the tray.
    auto iconArea = NamedChild(NamedChild(frame, L"RootGrid"), L"TaskbarFrameRepeater");
    if (!iconArea) { Restore(); return; }
    if (savedIconArea.get() != iconArea || quotaWindow != quota) {
        Restore();
        savedIconArea = winrt::make_weak(iconArea);
        savedWidth = iconArea.ReadLocalValue(FrameworkElement::WidthProperty());
        savedAlignment = iconArea.ReadLocalValue(FrameworkElement::HorizontalAlignmentProperty());
        quotaWindow = quota;
    }
    double want = content.ActualWidth() - width;
    if (iconArea.Width() != want) {
        RemoveProp(quota, kReady);
        iconArea.HorizontalAlignment(HorizontalAlignment::Left);
        iconArea.Width(want);
        appliedWidth = want;
        Wh_Log(L"Reserved %u DIPs; button area width %.1f", static_cast<unsigned>(width), want);
        return;  // XAML arranges asynchronously; setting Width is not completion.
    }
    if (iconArea.ActualWidth() < want - 0.5 || iconArea.ActualWidth() > want + 0.5) {
        RemoveProp(quota, kReady);
        return;
    }
    SetProp(quota, kReady, reinterpret_cast<HANDLE>(width));
}

void CALLBACK Timer(HWND, UINT, UINT_PTR, DWORD) {
    try { Tick(); }
    catch (winrt::hresult_error const& error) {
        Wh_Log(L"Reservation failed: %08X", error.code().value);
        try { Restore(); } catch (...) {}
    }
    catch (...) {
        Wh_Log(L"Reservation failed unexpectedly");
        try { Restore(); } catch (...) {}
    }
}

BOOL Wh_ModInit() {
    return HookTaskbarDllSymbols();
}

void Wh_ModAfterInit() {
    // Explorer can load the mod before creating its taskbar window.
    startupWorker = std::thread([] {
        while (!stopping) {
            HWND window = FindWindow(L"Shell_TrayWnd", nullptr);
            DWORD process{};
            GetWindowThreadProcessId(window, &process);
            if (window && process == GetCurrentProcessId()) {
                taskbarWindow = window;
                RunFromWindowThread(window, [](void*) {
                    timerId = SetTimer(nullptr, 0, 250, Timer);
                    Timer(nullptr, 0, 0, 0);
                }, nullptr);
                return;
            }
            std::this_thread::sleep_for(std::chrono::milliseconds(100));
        }
    });
}

void Wh_ModUninit() {
    stopping = true;
    if (startupWorker.joinable()) startupWorker.join();
    RunFromWindowThread(taskbarWindow, [](void*) {
        if (timerId) KillTimer(nullptr, timerId);
        timerId = 0;
        try { Restore(); } catch (...) {}
    }, nullptr);
}
