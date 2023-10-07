import configparser, os

config_file_path = 'pyhrpresence.ini'

def load_config(application_path):
    config = configparser.ConfigParser()
    config.read(os.path.join(application_path, config_file_path))
    return config


def get_config_value(config, section, key, default=None, save=True):
    if config.has_section(section) and config.has_option(section, key):
        value = config.get(section, key)
    else:
        value = default

    if save:
        set_config_value(config, section, key, str(value))

    return value

def set_config_value(config, section, key, value):
    if not config.has_section(section):
        config.add_section(section)
    config.set(section, key, value)

def save_config(config):
    with open(config_file_path, 'w') as configfile:
        config.write(configfile)