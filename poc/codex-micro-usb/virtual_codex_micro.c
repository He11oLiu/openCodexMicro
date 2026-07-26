#include <CoreFoundation/CoreFoundation.h>
#include <IOKit/hid/IOHIDKeys.h>
#include <IOKit/hidsystem/IOHIDUserDevice.h>
#include <dispatch/dispatch.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

/*
 * Disposable discovery probe for the native ChatGPT Codex Micro path.
 *
 * The descriptor exposes one vendor-defined application collection with
 * report ID 6 and 63-byte input/output payloads. node-hid therefore sees the
 * same 64-byte packets used by @worklouder/wl-device-kit.
 */
static const uint8_t report_descriptor[] = {
    0x06, 0x00, 0xff,       /* Usage Page (Vendor 0xff00) */
    0x09, 0x01,             /* Usage (1) */
    0xa1, 0x01,             /* Collection (Application) */
    0x85, 0x06,             /*   Report ID (6) */
    0x15, 0x00,             /*   Logical Minimum (0) */
    0x26, 0xff, 0x00,       /*   Logical Maximum (255) */
    0x75, 0x08,             /*   Report Size (8) */
    0x95, 0x3f,             /*   Report Count (63) */
    0x09, 0x01,             /*   Usage (1) */
    0x81, 0x02,             /*   Input (Data,Var,Abs) */
    0x95, 0x3f,             /*   Report Count (63) */
    0x09, 0x01,             /*   Usage (1) */
    0x91, 0x02,             /*   Output (Data,Var,Abs) */
    0xc0                    /* End Collection */
};

static void put_number(CFMutableDictionaryRef props, CFStringRef key, int value) {
    CFNumberRef number = CFNumberCreate(kCFAllocatorDefault, kCFNumberIntType, &value);
    CFDictionarySetValue(props, key, number);
    CFRelease(number);
}

static void print_report(uint32_t report_id, const uint8_t *report, CFIndex length) {
    fprintf(stderr, "SET report=%u length=%ld hex=", report_id, (long)length);
    for (CFIndex i = 0; i < length; i++) {
        fprintf(stderr, "%02x", report[i]);
    }
    fprintf(stderr, " text=");
    for (CFIndex i = 0; i < length; i++) {
        uint8_t ch = report[i];
        fputc(ch >= 32 && ch < 127 ? ch : '.', stderr);
    }
    fputc('\n', stderr);
    fflush(stderr);
}

int main(void) {
    CFMutableDictionaryRef props = CFDictionaryCreateMutable(
        kCFAllocatorDefault, 0,
        &kCFTypeDictionaryKeyCallBacks, &kCFTypeDictionaryValueCallBacks);
    CFDataRef descriptor = CFDataCreate(
        kCFAllocatorDefault, report_descriptor, sizeof(report_descriptor));

    CFDictionarySetValue(props, CFSTR(kIOHIDReportDescriptorKey), descriptor);
    CFDictionarySetValue(props, CFSTR(kIOHIDProductKey), CFSTR("Codex Micro discovery probe"));
    CFDictionarySetValue(props, CFSTR(kIOHIDManufacturerKey), CFSTR("Work Louder"));
    CFDictionarySetValue(props, CFSTR(kIOHIDSerialNumberKey), CFSTR("OPEN-CODEX-MICRO-POC"));
    CFDictionarySetValue(props, CFSTR(kIOHIDTransportKey), CFSTR("USB"));
    put_number(props, CFSTR(kIOHIDVendorIDKey), 0x303a);
    put_number(props, CFSTR(kIOHIDProductIDKey), 0x8360);
    put_number(props, CFSTR(kIOHIDVersionNumberKey), 0x0100);
    put_number(props, CFSTR(kIOHIDPrimaryUsagePageKey), 0xff00);
    put_number(props, CFSTR(kIOHIDPrimaryUsageKey), 1);

    IOHIDUserDeviceRef device = IOHIDUserDeviceCreateWithProperties(
        kCFAllocatorDefault, props, IOHIDUserDeviceOptionsCreateOnActivate);
    CFRelease(descriptor);
    CFRelease(props);

    if (device == NULL) {
        fprintf(stderr,
                "IOHIDUserDeviceCreateWithProperties failed; the virtual-HID "
                "entitlement was not granted.\n");
        return 2;
    }

    IOHIDUserDeviceRegisterSetReportBlock(
        device,
        ^IOReturn(IOHIDReportType type, uint32_t report_id,
                  const uint8_t *report, CFIndex length) {
            (void)type;
            print_report(report_id, report, length);
            return kIOReturnSuccess;
        });
    dispatch_queue_t queue = dispatch_queue_create(
        "dev.opencodexmicro.virtual-hid", DISPATCH_QUEUE_SERIAL);
    IOHIDUserDeviceSetDispatchQueue(device, queue);
    IOHIDUserDeviceSetCancelHandler(device, ^{
        CFRelease(device);
        exit(0);
    });
    IOHIDUserDeviceActivate(device);

    fprintf(stderr,
            "Virtual Codex Micro active (VID 303a PID 8360 usage ff00). "
            "Waiting for ChatGPT reports...\n");
    fflush(stderr);
    dispatch_main();
}
