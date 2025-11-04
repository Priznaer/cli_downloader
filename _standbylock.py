"""
A solution to keep windows a computer[windows/linux/mac] from going to sleep while an application is running.

Author :: 
    https://stackoverflow.com/users/6423074/pedro
Current_Version_Source :: 
    https://stackoverflow.com/a/61947613
Current_Version_Note :: 
    "There are still some caveats, as Linux may require sudo privileges and OS X (Darwin) is not tested yet."
"""



from functools import wraps
import platform

class MetaStandbyLock(type):
    """
    """

    SYSTEM = platform.system()

    def __new__(cls, name: str, bases: tuple, attrs: dict) -> type:
        if not ('inhibit' in attrs and 'release' in attrs):
            raise TypeError("Missing implementations for classmethods 'inhibit(cls)' and 'release(cls)'.")
        else:
            if name == 'StandbyLock':
                cls._superclass = super().__new__(cls, name, bases, attrs)
                return cls._superclass
            if cls.SYSTEM.upper() in name.upper():
                if not hasattr(cls, '_superclass'):
                    raise ValueError("Class 'StandbyLock' must be implemented.")
                cls._superclass._subclass = super().__new__(cls, name, bases, attrs)
                return cls._superclass._subclass
            else:
                return super().__new__(cls, name, bases, attrs)

class StandbyLock(metaclass=MetaStandbyLock):
    """
    """

    _subclass = None

    @classmethod
    def inhibit(cls):
        if cls._subclass is None:
            raise OSError(f"There is no 'StandbyLock' implementation for OS '{platform.system()}'.")
        else:
            return cls._subclass.inhibit()

    @classmethod
    def release(cls):
        if cls._subclass is None:
            raise OSError(f"There is no 'StandbyLock' implementation for OS '{platform.system()}'.")
        else:
            return cls._subclass.release()

    def __enter__(self, *args, **kwargs):
        self.inhibit()
        return self

    def __exit__(self, *args, **kwargs):
        self.release()

class WindowsStandbyLock(StandbyLock):
    """
    """

    ES_CONTINUOUS      = 0x80000000
    ES_SYSTEM_REQUIRED = 0x00000001

    INHIBIT = ES_CONTINUOUS | ES_SYSTEM_REQUIRED
    RELEASE = ES_CONTINUOUS

    @classmethod
    def inhibit(cls):
        import ctypes
        ctypes.windll.kernel32.SetThreadExecutionState(cls.INHIBIT)

    @classmethod
    def release(cls):
        import ctypes
        ctypes.windll.kernel32.SetThreadExecutionState(cls.RELEASE)

class LinuxStandbyLock(metaclass=MetaStandbyLock):
    """
    """

    COMMAND = 'systemctl'
    ARGS = ['sleep.target', 'suspend.target', 'hibernate.target', 'hybrid-sleep.target']

    @classmethod
    def inhibit(cls):
        import subprocess
        subprocess.run([cls.COMMAND, 'mask', *cls.ARGS])

    @classmethod
    def release(cls):
        import subprocess
        subprocess.run([cls.COMMAND, 'unmask', *cls.ARGS])

class DarwinStandbyLock(metaclass=MetaStandbyLock):
    """
    """

    COMMAND = 'caffeinate'
    BREAK = b'\003'

    _process = None

    @classmethod
    def inhibit(cls):
        from subprocess import Popen, PIPE
        cls._process = Popen([cls.COMMAND], stdin=PIPE, stdout=PIPE)

    @classmethod
    def release(cls):
        cls._process.stdin.write(cls.BREAK)
        cls._process.stdin.flush()
        cls._process.stdin.close()
        cls._process.wait()

def standby_lock(callback):
    """ standby_lock(callable) -> callable
        This decorator guarantees that the system will not enter standby mode while 'callable' is running.
    """
    @wraps(callback)
    def new_callback(*args, **kwargs):
        with StandbyLock():
            return callback(*args, **kwargs)
    return new_callback





############################# Usage Note (by author) #############################
"""
Based on multiple approaches I've found throughout the internet I've come up with this module below. Special thanks to @mishsx for the Windows workaround.

Using it is very simple. You may opt for the decorator approach using standby_lock or via StandbyLock as a context manager:
"""
## decorator
@standby_lock
def foo(*args, **kwargs):
    # do something lazy here...
    pass

## context manager
with StandbyLock():
    # ...or do something lazy here instead
    pass

# While foo is executing your system will stay awake.
#################################################################################