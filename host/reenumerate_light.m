// One-shot recovery of the configured USB device. IOKit performs the same
// identity match even if the device list changes after the daemon's lookup.
#import <Foundation/Foundation.h>
#import <IOKit/IOCFPlugIn.h>
#import <IOKit/usb/IOUSBLib.h>

int main(int argc, const char **argv) {
    @autoreleasepool {
        if (argc != 2 || !argv[1][0]) return 1;
        NSString *serial = [NSString stringWithUTF8String:argv[1]];
        if (!serial) return 1;
        NSMutableDictionary *match = CFBridgingRelease(IOServiceMatching("IOUSBHostDevice"));
        match[@kIOPropertyMatchKey] = @{@"idVendor": @0x303a, @"idProduct": @0x1001,
                                        @"USB Serial Number": serial};
        io_iterator_t devices;
        if (IOServiceGetMatchingServices(kIOMainPortDefault, CFBridgingRetain(match), &devices)
            != KERN_SUCCESS) return 2;
        io_service_t device = IOIteratorNext(devices);
        io_service_t duplicate = IOIteratorNext(devices);
        IOObjectRelease(devices);
        if (!device || duplicate) {
            if (device) IOObjectRelease(device);
            if (duplicate) IOObjectRelease(duplicate);
            return 3;
        }

        IOCFPlugInInterface **plugin = NULL;
        SInt32 score;
        IOReturn status = IOCreatePlugInInterfaceForService(device, kIOUSBDeviceUserClientTypeID,
                                                            kIOCFPlugInInterfaceID, &plugin, &score);
        IOObjectRelease(device);
        if (status != kIOReturnSuccess || !plugin) return 4;
        IOUSBDeviceInterface197 **usb = NULL;
        HRESULT result = (*plugin)->QueryInterface(plugin, CFUUIDGetUUIDBytes(kIOUSBDeviceInterfaceID197),
                                                   (LPVOID *)&usb);
        (*plugin)->Release(plugin);
        if (result != S_OK || !usb) return 5;
        status = (*usb)->USBDeviceOpen(usb);
        if (status == kIOReturnSuccess) {
            status = (*usb)->USBDeviceReEnumerate(usb, 0);
            (*usb)->USBDeviceClose(usb);
        }
        (*usb)->Release(usb);
        if (status != kIOReturnSuccess) fprintf(stderr, "USB re-enumeration failed (0x%x)\n", status);
        return status == kIOReturnSuccess ? 0 : 6;
    }
}
