import argparse
import logging
import yaml
from time import sleep
from importlib import import_module

import paho.mqtt.client as mqtt

from pi_mqtt_gpio.modules import PinPullup, PinDirection


RECONNECT_DELAY_SECS = 5
GPIOS = {}
LAST_STATES = {}

_LOG = logging.getLogger(__name__)
_LOG.addHandler(logging.StreamHandler())
_LOG.setLevel(logging.DEBUG)


def on_disconnect(client, userdata, rc):
    _LOG.warning("Disconnected from MQTT server with code: %s" % rc)
    while rc != 0:
        sleep(RECONNECT_DELAY_SECS)
        rc = client.reconnect()


def install_missing_requirements(module):
    try:
        reqs = getattr(module, "REQUIREMENTS")
    except AttributeError:
        _LOG.info("Module %r has no extra requirements to install." % module)
        return
    import pkg_resources
    installed = pkg_resources.WorkingSet()
    not_installed = []
    for req in reqs:
        if installed.find(pkg_resources.Requirement.parse(req)) is None:
            not_installed.append(req)
    if not_installed:
        import pip
        pip.main(["install"] + not_installed)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("config")
    args = p.parse_args()

    with open(args.config) as f:
        config = yaml.load(f)

    client = mqtt.Client()
    user = config["mqtt"].get("user")
    password = config["mqtt"].get("password")
    topic_prefix = config["mqtt"]["topic_prefix"].rstrip("/")

    if user and password:
        client.username_pw_set(user, password)

    def on_conn(client, userdata, flags, rc):
        for output_config in config.get("digital_outputs", []):
            topic = "%s/%s" % (topic_prefix, output_config["subtopic"])
            client.subscribe(topic, qos=1)
            _LOG.info("Subscribed to topic: %r", topic)

    def on_msg(client, userdata, msg):
        msg.payload = msg.payload.decode("utf-8")
        _LOG.info("Got message on topic %r: %r", msg.topic, msg.payload)
        #Got message on topic 'home/brewery/HLT/Element': b'off'
        #....No output found with name of '/'
        #_LOG.info(len("%s" % topic_prefix))
        output_name = msg.topic[len("%s" % topic_prefix)+1:]
        #_LOG.info("output_name: %r", output_name)
        output_config = None
        for output in config.get("digital_outputs", []):
            if output["subtopic"] == output_name:
                output_config = output
                #_LOG.info("....Setting output_config: %r", output_config)
        if output_config is None:
            _LOG.warning("....No output found with name of %r", output_name)
            return
        #output_value = msg.payload[1:]
        #_LOG.info(".... %r", msg.payload)
        if msg.payload not in ( output_config["on_payload"], output_config["off_payload"]):
            _LOG.info("....Payload does not relate to configured on/off values: %r", msg.payload)
            return
        value = msg.payload == output_config["on_payload"]
        #_LOG.info("....Set value %r to %r", msg.payload, value)
        gpio = GPIOS[output_config["module"]]
        #_LOG.info("....Set gpio: ", gpio)
        gpio.set_pin(output_config["pin"], value)
        #_LOG.info("....Set output %r to %r", output_config["name"], value)
        topic = "%s/%s" % (topic_prefix, output_config["subtopic"])
        #client.publish(topic, payload=msg.payload)
        #client.publish("....%s/%s" % (topic_prefix, output_name), payload=msg.payload)
        #_LOG.info("....Publish %r : %r", output_name, msg.payload)

    client.on_disconnect = on_disconnect
    client.on_connect = on_conn
    client.on_message = on_msg

    for gpio_config in config["gpio_modules"]:
        gpio_module = import_module("pi_mqtt_gpio.modules.%s" % gpio_config["module"])
        install_missing_requirements(gpio_module)
        GPIOS[gpio_config["name"]] = gpio_module.GPIO(gpio_config)

    for input_config in config.get("digital_inputs", []):
        pud = None
        if input_config["pullup"]:
            pud = PinPullup.UP
        elif input_config["pulldown"]:
            pud = PinPullup.DOWN

        gpio = GPIOS[input_config["module"]]
        gpio.setup_pin(
            input_config["pin"], PinDirection.INPUT, pud, input_config)
        LAST_STATES[input_config["name"]] = None

    for output_config in config.get("digital_outputs", []):
        gpio = GPIOS[output_config["module"]]
        gpio.setup_pin(output_config["pin"], PinDirection.OUTPUT, None, output_config)

    client.connect(config["mqtt"]["host"], config["mqtt"]["port"], 30)
    client.loop_start()

    try:
        while True:
            for input_config in config.get("digital_inputs", []):
                gpio = GPIOS[input_config["module"]]
                state = bool(gpio.get_pin(input_config["pin"]))
                sleep(0.05)
                if bool(gpio.get_pin(input_config["pin"])) != state:
                    continue
                if state != LAST_STATES[input_config["name"]]:
                    _LOG.info("Input %r state changed to %r", input_config["name"], state)
                    client.publish( "%s/input/%s" % (topic_prefix, input_config["name"]),
                        payload=(input_config["on_payload"] if state
                                 else input_config["off_payload"]))
                    LAST_STATES[input_config["name"]] = state
            sleep(0.05)
    except KeyboardInterrupt:
        print ("")
    finally:
        client.disconnect()
        client.loop_stop()
