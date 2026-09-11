// Exercise the production selector with deterministic window API responses.
// No actual windows, processes, taskbar or Explorer state are changed.
#include <windows.h>
#include <cassert>
#include <cwchar>
#include <cstdio>
#include <initializer_list>

struct Window { ULONG_PTR width{}, lease{}; } windows[4];
ULONG_PTR now = 100000;
int enumerations = 0;
HWND handle(size_t index) { return reinterpret_cast<HWND>(index); }
HANDLE TestGetProp(HWND window, LPCWSTR name) {
    auto index = reinterpret_cast<size_t>(window);
    if (index >= 4) return nullptr;
    auto value = std::wcscmp(name, L"aifuel.TaskbarWidth.v1") == 0
        ? windows[index].width : windows[index].lease;
    return reinterpret_cast<HANDLE>(value);
}
ULONGLONG TestTickCount() { return now; }
BOOL TestEnumWindows(WNDENUMPROC callback, LPARAM parameter) {
    ++enumerations;
    for (size_t index = 1; index < 4; ++index) {
        if (!callback(handle(index), parameter)) return FALSE;
    }
    return TRUE;
}

#define GetPropW TestGetProp
#define GetTickCount64 TestTickCount
#define EnumWindows TestEnumWindows
#include "../taskbar_lease.h"

int main() {
    // Window 1 is a menu/tooltip with the same title, above the real panel (2).
    windows[2] = {415, now};
    assert(FindQuotaWindow(nullptr) == handle(2));
    int scanned = enumerations;
    assert(FindQuotaWindow(handle(2)) == handle(2));
    assert(enumerations == scanned);  // A popup cannot replace the live owner.

    // A recreated quota window replaces the old expired handle.
    now += 2001;
    windows[3] = {625, now};
    assert(FindQuotaWindow(handle(2)) == handle(3));
    windows[3].width = 0;  // Switching to floating releases the lease.
    assert(FindQuotaWindow(handle(3)) == nullptr);

    for (ULONG_PTR width : {ULONG_PTR(63), ULONG_PTR(1601)}) {
        windows[2] = {width, now};
        assert(FindQuotaWindow(nullptr) == nullptr);
    }
    windows[2] = {415, now + 1};
    assert(FindQuotaWindow(nullptr) == nullptr);  // Future/invalid timestamps.
    windows[2] = {64, now - 2000};
    assert(FindQuotaWindow(nullptr) == handle(2));
    ++now;
    assert(FindQuotaWindow(handle(2)) == nullptr);
    windows[2] = {1600, now};
    assert(FindQuotaWindow(nullptr) == handle(2));
    std::puts("Native lease selection, popup, recreation and expiry checks passed.");
}
