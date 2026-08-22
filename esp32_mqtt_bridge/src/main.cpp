#include "BLEDevice.h"
#include "BLEUtils.h"
#include "BLEServer.h"
#include "BLEBeacon.h"
#include <WiFi.h>
#include <ArduinoHA.h>
#include <string>
#include <vector>
#include <cstdio>
#include "esp_gap_ble_api.h"

//////////////////////////////////////////////////////
//CONFIGURATION — everything mesh-specific lives in config.h
//////////////////////////////////////////////////////
// `PreconfiguredLight` struct must exist before config.h (which uses it).
struct PreconfiguredLight {
  uint8_t number;
  uint8_t type[2];
  const char* id;
  const char* name;
};

// Pull in the per-mesh settings (key, MQTT/WiFi, lights, beacon UUID) for the
// BRMESH_MESH selected on the compiler command line.
#include "config.h"

// `PreconfiguredLight` is also referenced after the include by the mesh blocks;
// keep the struct definition here (before config.h) so both can use it.
//////////////////////////////////////////////////////
//END
//////////////////////////////////////////////////////

uint8_t my_key[4];
byte mac[6];
WiFiClient client;
HADevice device;
HAMqtt* mqtt;
BLEAdvertising* pAdvertising;
const uint8_t default_key[] = { 0x5e, 0x36, 0x7b, 0xc4 };
struct LightType {
  std::string name;
  uint8_t code[2];
};
std::vector<LightType> lightTypes = {
  {"Smart", {0x39, 0xae}}, // 44601 - AE39 - smart light
  {"RGBW" , {0xa1, 0xa8}}, // 43169 - A8A1 - RGBW light
  {"RGB"  , {0xa0, 0xa8}}, // 43168 - A8A0 - RGB light
};
#define BLESCAN_DURATION 5
struct LightDevice {
  BLEAdvertisedDevice device;
  uint8_t type[2];
  bool isRegistered = false;
  std::string id;
  HALight* light;
  std::string name;
  uint8_t number;
};
std::vector<LightDevice> myLights;
#define BUFFER_SIZE 200
char str_buffer[BUFFER_SIZE];
BLEScan* pBLEScan;
const int ledPin = 2;

// "All lights" on/off switch entity (group all-on/all-off)
HASwitch* allSwitch = nullptr;
// "All lights" group brightness slider entity (group D5 family)
HANumber* groupBrightness = nullptr;
// "All lights" group color entity (sends a family 0x05 group-color command)
HALight* groupColor = nullptr;

bool doesStringMatchBytes(std::string str, const u_int8_t* bytes) {
  bool result = true;
  for (int i = 0; i < str.length(); i++) {
    if (str[i] != bytes[i]) {
      result = false;
      break;
    }
  }
  return result;
}

uint8_t SEND_SEQ = 0;
uint8_t SEND_COUNT = 1;
const uint8_t DEFAULT_BLE_FASTCON_ADDRESS[] = { 0xC1, 0xC2, 0xC3 };
const uint8_t addrLength = 3;

void dump(const uint8_t* data, int length)
{
  for (int i = 0; i < length; i++)
  {
    Serial.printf("%2.2X", data[i]);
  }
}

void dump(std::string str)
{
  for (int i = 0; i < str.size(); i++)
  {
    Serial.printf("%2.2X", str[i]);
  }
}

uint8_t package_ble_fastcon_body(int i, int i2, uint8_t sequence, uint8_t safe_key, int forward, const uint8_t* data, int length, const uint8_t* key, uint8_t*& payload)
{
  if (length > 12)
  {
    Serial.print("data too long");
    payload = 0;
    return 0;
  }
  uint8_t payloadLength = 4 + 12;
  payload = (uint8_t*)malloc(payloadLength);
  payload[0] = (i2 & 0b1111) << 0 | (i & 0b111) << 4 | (forward & 0xff) << 7;
  payload[1] = sequence & 0xff;
  payload[2] = safe_key;
  payload[3] = 0; // checksum
  // fill payload with zeros
  for (int j = 4; j < payloadLength; j++) payload[j]=0;
  memcpy(payload + 4, data, length);

  uint8_t checksum = 0;
  for (int j = 0; j < length + 4; j++)
  {
    if (j == 3) continue;
    checksum = (checksum + payload[j]) & 0xff;
  }
  payload[3] = checksum;
  for (int j = 0; j < 4; j++) {
    payload[j] = default_key[j & 3] ^ payload[j];
  }
  for (int j = 0; j < 12; j++) {
    payload[4 + j] = key[j & 3] ^ payload[4 + j];
  }
  return payloadLength;
}

uint8_t get_payload_with_inner_retry(int i, const uint8_t* data, int length, int i2, const uint8_t* key, int forward, uint8_t*& payload) {
  SEND_COUNT++;
  SEND_SEQ = SEND_COUNT;
  Serial.print("data: "); dump(data, length); Serial.print("\n");
  Serial.print("key: "); dump(key, 4); Serial.print("\n");
  Serial.printf("sequence: %d\n", SEND_SEQ);
  uint8_t safe_key = 0xff;
  bool hasKey = false;
  for (int i = 0; i < 4; i++) {
    if (key[i] != 0) {
      hasKey = true;
      break;
    }
  }
  if (hasKey) safe_key = key[3];
  uint8_t result = package_ble_fastcon_body(i, i2, SEND_SEQ, safe_key, forward, data, length, key, payload);
  if (!hasKey) {
    // set the data content to the default key
    for (int i = 4; i < 16; i++) {
      payload[i] = default_key[i & 3];
    }
  }
  return result;
}

void whiteningInit(uint8_t val, uint8_t* ctx)
{
  ctx[0] = 1;
  ctx[1] = (val >> 5) & 1;
  ctx[2] = (val >> 4) & 1;
  ctx[3] = (val >> 3) & 1;
  ctx[4] = (val >> 2) & 1;
  ctx[5] = (val >> 1) & 1;
  ctx[6] = val & 1;
}

void whiteningEncode(const uint8_t* data, int len, uint8_t* ctx, uint8_t* result)
{
  memcpy(result, data, len);
  for (int i = 0; i < len; i++) {
    int ctx3 = ctx[3];
    int ctx5 = ctx[5];
    int ctx6 = ctx[6];
    int ctx4 = ctx[4];
    int ctx52 = ctx5 ^ ctx[2];
    int ctx41 = ctx4 ^ ctx[1];
    int ctx63 = ctx6 ^ ctx3;
    int ctx630 = ctx63 ^ ctx[0];

    int c = result[i];
    result[i] = ((c & 0x80) ^ ((ctx52 ^ ctx6) << 7))
      + ((c & 0x40) ^ (ctx630 << 6))
      + ((c & 0x20) ^ (ctx41 << 5))
      + ((c & 0x10) ^ (ctx52 << 4))
      + ((c & 0x08) ^ (ctx63 << 3))
      + ((c & 0x04) ^ (ctx4 << 2))
      + ((c & 0x02) ^ (ctx5 << 1))
      + ((c & 0x01) ^ (ctx6 << 0));

    ctx[2] = ctx41;
    ctx[3] = ctx52;
    ctx[4] = ctx52 ^ ctx3;
    ctx[5] = ctx630 ^ ctx4;
    ctx[6] = ctx41 ^ ctx5;
    ctx[0] = ctx52 ^ ctx6;
    ctx[1] = ctx630;
  }
}

uint8_t reverse_8(uint8_t d)
{
  uint8_t result = 0;
  for (uint8_t k = 0; k < 8; k++) {
    result |= ((d >> k) & 1) << (7 - k);
  }
  return result;
}

uint16_t reverse_16(uint16_t d) {
  uint16_t result = 0;
  for (uint8_t k = 0; k < 16; k++) {
    result |= ((d >> k) & 1) << (15 - k);
  }
  return result;
}

uint16_t crc16(const uint8_t* addr, const uint8_t* data, uint8_t dataLength)
{
  uint16_t crc = 0xffff;
  for (int8_t i = addrLength - 1; i >= 0; i--)
  {
    crc ^= addr[i] << 8;
    for (uint8_t ii = 0; ii < 4; ii++) {
      uint16_t tmp = crc << 1;
      if ((crc & 0x8000) != 0) tmp ^= 0x1021;
      crc = tmp << 1;
      if ((tmp & 0x8000) != 0) crc ^= 0x1021;
    }
  }
  for (uint8_t i = 0; i < dataLength; i++) {
    crc ^= reverse_8(data[i]) << 8;
    for (uint8_t ii = 0; ii < 4; ii++) {
      uint16_t tmp = crc << 1;
      if ((crc & 0x8000) != 0) tmp ^= 0x1021;
      crc = tmp << 1;
      if ((tmp & 0x8000) != 0) crc ^= 0x1021;
    }
  }
  crc = ~reverse_16(crc) & 0xffff;
  return crc;
}

uint8_t get_rf_payload(const uint8_t* addr, const uint8_t* data, uint8_t dataLength, uint8_t*& rfPayload)
{
  uint8_t data_offset = 0x12;
  uint8_t inverse_offset = 0x0f;
  uint8_t result_data_size = data_offset + addrLength + dataLength+2;
  uint8_t* resultbuf = (uint8_t*)malloc(result_data_size);
  memset(resultbuf, 0, result_data_size);

  resultbuf[0x0f] = 0x71;
  resultbuf[0x10] = 0x0f;
  resultbuf[0x11] = 0x55;

  for (uint8_t j = 0; j < addrLength; j++) {
    resultbuf[data_offset + addrLength - j - 1] = addr[j];
  }

  for (int j = 0; j < dataLength; j++) {
    resultbuf[data_offset + addrLength + j] = data[j];
  }

  for (int i = inverse_offset; i < inverse_offset + addrLength + 3; i++) {
    resultbuf[i] = reverse_8(resultbuf[i]);
  }

  int crc = crc16(addr, data, dataLength);
  resultbuf[result_data_size-2] = crc & 0xff;
  resultbuf[result_data_size-1] = (crc >> 8) & 0xff;
  rfPayload = resultbuf;
  return result_data_size;
}

uint8_t do_generate_command(int i, const uint8_t* data, uint8_t length, const uint8_t* key, int forward, int use_default_adapter, int i2, uint8_t*& rfPayload)
{
  if (i2 < 0) i2 = 0;
  uint8_t* payload = 0;
  uint8_t payloadLength = get_payload_with_inner_retry(i, data, length, i2, key, forward, payload);
  uint8_t* rfPayloadTmp = 0;
  Serial.print("payload: "); dump(payload, payloadLength); Serial.print("\n");
  uint8_t rfPayloadLength = get_rf_payload(DEFAULT_BLE_FASTCON_ADDRESS, payload, payloadLength, rfPayloadTmp);
  free(payload);
  uint8_t ctx[7];
  whiteningInit(0x25, &ctx[0]);
  uint8_t* result = (uint8_t*)malloc(rfPayloadLength);
  whiteningEncode(rfPayloadTmp, rfPayloadLength, ctx, result);
  rfPayload = (uint8_t*)malloc(rfPayloadLength-15);
  memcpy(rfPayload, result + 15, rfPayloadLength - 15);
  Serial.print("rf payload: "); dump(rfPayload, rfPayloadLength-15); Serial.print("\n");
  free(result);
  free(rfPayloadTmp);
  return rfPayloadLength-15;
}

std::string getServiceData(uint8_t rfPayloadLength, uint8_t* rfPayload)
{
  uint8_t ble_adv_data[] = { 0x02, 0x01, 0x1A, 0x1B, 0xFF, 0xF0, 0xFF };
  uint8_t* advPacket = (uint8_t*)malloc(rfPayloadLength + sizeof(ble_adv_data));
  memcpy(advPacket, ble_adv_data, sizeof(ble_adv_data));
  memcpy(advPacket + sizeof(ble_adv_data), rfPayload, rfPayloadLength);
  Serial.print("send: "); dump(advPacket, rfPayloadLength + sizeof(ble_adv_data)); Serial.print("\n");
  uint8_t dataLength = rfPayloadLength + sizeof(ble_adv_data);
  std::string serviceData = "";
  serviceData += (char)(dataLength-4);
  for (int i = 4; i < dataLength; i++) serviceData += (char)advPacket[i];
  free(advPacket);
  return serviceData;
}

void single_control(const uint8_t* key, const uint8_t* data)
{
  uint8_t* rfPayload = 0;
  uint8_t rfPayloadLength = do_generate_command(5, data, 8, key, true /* forward */, true /* use_default_adapter */, 0, rfPayload);
  std::string serviceData = getServiceData(rfPayloadLength, rfPayload);
  BLEAdvertisementData oAdvertisementData = BLEAdvertisementData();
  oAdvertisementData.setFlags(0x04); // BR_EDR_NOT_SUPPORTED 0x04
  oAdvertisementData.addData(serviceData);
  pAdvertising->setAdvertisementData(oAdvertisementData);
  pAdvertising->setMinInterval(50);
  pAdvertising->setMaxInterval(50);
  pAdvertising->start();
  delay(250);
  pAdvertising->stop();
  Serial.println("");
}

// Use a fresh RANDOM (non-resolvable) BLE address for the next advertisement.
// The BRMesh phone app randomizes its MAC on every packet, so the sniffer's
// "seen this MAC already -> drop" duplicate filter never suppresses it. Our
// bridge historically used its fixed PUBLIC MAC, so after the sniffer's first
// sighting every repeat frame from that MAC was silently dropped and manual
// `allon`/`alloff` presses never appeared. Setting a new random address before
// each burst makes the bridge look like a brand-new advertiser every time,
// exactly like the phone -> every command reproducibly shows up on the sniffer.
void setRandomAdvAddress()
{
  esp_bd_addr_t randAddr;
  // ESP32's hardware RNG generates the 6 random bytes.
  esp_fill_random(randAddr, 6);
  // The BRMesh sniffer's duplicate filter keys on MAC, but it forwards any
  // frame whose payload contains FF F0 FF (the BRMesh manufacturer pattern),
  // independent of the MAC. So any random address is acceptable. Mark it as a
  // RANDOM STATIC address (b10 in the two MSBs of the first byte, i.e. 0xC0),
  // the standard non-resolvable type that legacy ADV_TYPE_IND supports.
  randAddr[0] = (randAddr[0] & 0x3F) | 0xC0;
  esp_err_t er = esp_ble_gap_set_rand_addr(randAddr);
  if (er != ESP_OK) {
    Serial.printf("setRandomAdvAddress: esp_ble_gap_set_rand_addr -> 0x%x\n", er);
  }
}

// Advertise an app-style (A424E4...) command using the STRUCTURED legacy GAP
// config so the advertisement is definitely LEGACY (ADV_TYPE_IND), not BLE5
// extended advertising (which legacy scanners/lights cannot receive).
// On-air packet: [flags 02 01 02][manufacturer <len> FF F0 FF <rfPayload>]
void advertiseAppPayload(const uint8_t* data, uint8_t length, bool continuous)
{
  uint8_t* rfPayload = 0;
  // Match the phone app EXACTLY: Fastcon header byte0 = forward(1)<<7 | cmd(5)<<4 | i2(0)
  // => decrypted 0xD0, on-air body[0]=0x8E. Earlier this used cmd=3/forward=0/i2=3
  // (header 0x33), which the floodlights SILENTLY IGNORE despite a clean decode.
  // cmd=5/forward=1 is the form the app uses (VALIDATED on-air 2026-08-22: lights respond).
  uint8_t rfPayloadLength = do_generate_command(5, data, length, my_key, 1 /*forward*/, 1 /*use_default_adapter*/, 0 /*i2*/, rfPayload);

  // manufacturer data = company 0xfff0 (F0 FF) + rfPayload
  uint8_t mdata[31];
  size_t m = 0;
  mdata[m++] = 0xF0; mdata[m++] = 0xFF;
  memcpy(mdata + m, rfPayload, rfPayloadLength); m += rfPayloadLength;
  free(rfPayload);

  Serial.print("adv: 020102");
  if ((m + 1) < 0x10) Serial.print('0');
  Serial.print(m + 1, HEX); Serial.print("FF");
  for (size_t i = 0; i < m; i++) {
    if (mdata[i] < 0x10) Serial.print('0');
    Serial.print(mdata[i], HEX);
  }
  Serial.printf(" (len=%d)\n", (int)(3 + m + 2));

  esp_ble_adv_data_t advData = {};
  advData.set_scan_rsp = false;
  advData.include_name = false;
  advData.include_txpower = false;
  advData.min_interval = 0x20;
  advData.max_interval = 0x40;
  advData.flag = ESP_BLE_ADV_FLAG_GEN_DISC;   // 0x02 LE General Discoverable
  advData.manufacturer_len = m;
  advData.p_manufacturer_data = mdata;
  esp_ble_gap_config_adv_data(&advData);

  esp_ble_adv_params_t p = {};
  p.adv_int_min = 0x20;
  p.adv_int_max = 0x40;
  p.adv_type = ADV_TYPE_IND;                 // LEGACY connectable undirected
  p.own_addr_type = BLE_ADDR_TYPE_RANDOM;    // fresh random MAC per burst (see
                                             // setRandomAdvAddress) so the
                                             // sniffer sees us as a new device
  p.channel_map = ADV_CHNL_ALL;
  p.adv_filter_policy = ADV_FILTER_ALLOW_SCAN_ANY_CON_ANY;
  setRandomAdvAddress();                     // install the new random MAC first
  esp_ble_gap_start_advertising(&p);

  if (!continuous) {
    // Short burst: the floodlight reacts to a single packet, so ~300ms at the
    // 20-40ms adv interval is plenty (a handful of repeats for reliability).
    // Was 1500ms -> that put ~50 duplicate packets on the air per command,
    // flooding the sniffer log and blocking the loop. Shorter = faster + quieter.
    delay(300);
    esp_ble_gap_stop_advertising();
  }
  Serial.println("");
}

// Send an app-style (A424E4...) 12-byte command to the mesh (short burst).
void sendAppCommand(const uint8_t* data, uint8_t length)
{
  advertiseAppPayload(data, length, false);
}

// Group "all lights" on/off (family 0xD5, group 42).
// PLAINTEXT fed to package_ble_fastcon_body (which encrypts it with the mesh
// key from config.h). data[0]=0x43 (group command), data[1]=0x2A (group 42),
// data[2]=0xA8, data[4]=0x80=ON / 0x00=OFF.
void onAllSwitchCommand(bool state, HASwitch* sender)
{
  Serial.print("All lights: ");
  Serial.println(state ? "ON" : "OFF");
  uint8_t data[12] = {
    0x43, 0x2A, 0xA8, 0x00,
    (uint8_t)(state ? 0x80 : 0x00),   // ON / OFF
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00
  };
  sendAppCommand(data, 12);
  sender->setState(state); // report back to Home Assistant
}

// Group brightness (family 0xD5, group 42). PLAINTEXT: data[4] = level (1..127
// implies ON, 0 = OFF).
void sendGroupBrightness(uint8_t pct)
{
  if (pct > 100) pct = 100;
  uint8_t level = (pct == 0) ? 0x00 : (uint8_t)((pct * 127) / 100);
  uint8_t data[12] = {
    0x43, 0x2A, 0xA8, 0x00, level,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00
  };
  sendAppCommand(data, 12);
}

// Home Assistant callback: "All Lights Brightness" slider (0..100%).
void onGroupBrightnessCommand(HANumeric number, HANumber* sender)
{
  uint8_t pct = number.toUInt8();
  Serial.print("All lights brightness: ");
  Serial.println(pct);
  sendGroupBrightness(pct);
  sender->setState(number); // report back to Home Assistant
}

LightDevice getLight(std::string id)
{
  for (int i = 0; i < myLights.size(); i++) {
    if (myLights[i].id == id) return myLights[i];
  }
  throw std::runtime_error("Light not found");
}

void onStateCommand(bool state, HALight* sender)
{
  Serial.print("Light: ");
  Serial.println(sender->getName());
  Serial.print("ID: ");
  Serial.println(sender->uniqueId());
  Serial.print("State: ");
  Serial.println(state);
  LightDevice light = getLight(sender->uniqueId());
  // Single-light brightness/state (family 0xB4). PLAINTEXT (app ground truth,
  // key-agnostic): data[0]=0x22, data[1]=device RAW, data[2]=ON(0x80)|level RAW.
  uint8_t level = sender->getCurrentBrightness() & 0x7F;
  uint8_t data[12] = {
    0x22,
    light.number,
    (uint8_t)((state ? 0x80 : 0x00) | level),
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00
  };
  sendAppCommand(data, 12);
  sender->setState(state); // report state back to the Home Assistant
}

void onBrightnessCommand(uint8_t brightness, HALight* sender)
{
  Serial.print("Light: ");
  Serial.println(sender->getName());
  Serial.print("ID: ");
  Serial.println(sender->uniqueId());
  Serial.print("Brightness: ");
  Serial.println(brightness);
  LightDevice light = getLight(sender->uniqueId());
  // Single-light brightness (family 0xB4), ON bit set. PLAINTEXT raw:
  // data[0]=0x22, data[1]=device, data[2]=0x80|brightness.
  uint8_t data[12] = {
    0x22,
    light.number,
    (uint8_t)(0x80 | (brightness & 0x7F)),
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00
  };
  sendAppCommand(data, 12);
  sender->setBrightness(brightness); // report brightness back to the Home Assistant
}

void onColorTemperatureCommand(uint16_t temperature, HALight* sender)
{
  Serial.print("Light: ");
  Serial.println(sender->getName());
  Serial.print("ID: ");
  Serial.println(sender->uniqueId());
  Serial.print("Color temperature: ");
  Serial.println(temperature);
  LightDevice light = getLight(sender->uniqueId());
  uint8_t data[] = { 0x72, 0x00, 0xff, 0x00, 0x00, 0x00, 0x00, 0x00 };
  data[1] = light.number;
  data[2] = sender->getCurrentBrightness() & 127;
  data[6] = temperature & 127;
  data[7] = (temperature >> 8) & 127;
  single_control(my_key, data);
  sender->setColorTemperature(temperature); // report color temperature back to the Home Assistant
}

void onRGBColorCommand(HALight::RGBColor color, HALight* sender)
{
  Serial.print("Light: ");
  Serial.println(sender->getName());
  Serial.print("ID: ");
  Serial.println(sender->uniqueId());
  Serial.print("Red: ");
  Serial.println(color.red);
  Serial.print("Green: ");
  Serial.println(color.green);
  Serial.print("Blue: ");
  Serial.println(color.blue);
  LightDevice light = getLight(sender->uniqueId());
  // Single-light color (family 0xE4). PLAINTEXT (app ground truth, key-agnostic):
  //   data[0]=0x72, data[1]=device RAW, data[2]=0x80|level RAW,
  //   data[3]=blue, data[4]=red, data[5]=green (direct channel values)
  // Brightness: if the light has no brightness set yet (HA state is 0), default
  // to 100% (level 127) so a bare color command turns the light on fully, like
  // the app does. Once a brightness is set it is used as-is.
  uint8_t level = sender->getCurrentBrightness() & 0x7F;
  if (level == 0) level = 0x7F; // default 100%
  uint8_t data[12] = {
    0x72,
    light.number,
    (uint8_t)(0x80 | level),
    color.blue, color.red, color.green,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00
  };
  sendAppCommand(data, 12);
  sender->setRGBColor(color); // report color back to the Home Assistant
}

// Group "all lights" color (family 0x05, group 42).
// PLAINTEXT fed to sendAppCommand (which encrypts). Decrypted ground truth from
// the app capture: p6=0x05, p7=0x2D (group 42 ^ 0x07, same as all_lights D5),
// p10=0xCA(ON 100%), p11=BLUE^F3, tail0=RED^48, tail1=GREEN^95,
// tail2=WHITE^C3, tail3=WHITE2^6C.
// So plaintext: data[0]=0x05^0x96=0x93, data[1]=0x2A (raw group id, like the
// allSwitch command), data[4]=0xCA^0x35=0xFF, data[5..9]=raw blue/red/green/white.
// Mapping from HA RGB (r,g,b): all-zero -> white channel on (like single-light W),
// otherwise red=r, green=g, blue=b with white off.
void onGroupColorCommand(HALight::RGBColor color, HALight* sender)
{
  Serial.print("All lights color R/G/B: ");
  Serial.printf("%d,%d,%d\n", color.red, color.green, color.blue);
  uint8_t group = 0x2A; // "All" group (42)
  bool white = (color.red == 0 && color.green == 0 && color.blue == 0);
  uint8_t data[12] = {
    0x93,                    // family 0x05 ^ 0x96
    group,                   // raw group id 0x2A (encryption adds the ^0x07)
    0x00, 0x00,              // p8, p9 unused
    0xFF,                    // state+brightness 0xCA ^ 0x35 (ON 100%)
    color.blue,              // p11 = BLUE ^ 0xF3
    color.red,               // tail0 = RED ^ 0x48
    color.green,             // tail1 = GREEN ^ 0x95
    (uint8_t)(white ? 0x7F : 0x00), // tail2 = WHITE ^ 0xC3 (0x7F=on)
    0x00,                    // tail3 = WHITE2 ^ 0x6C
    0x00, 0x00
  };
  sendAppCommand(data, 12);
  sender->setRGBColor(color);
}

class AddDeviceCallback: public BLEAdvertisedDeviceCallbacks
{
  void onResult(BLEAdvertisedDevice foundDevice)
  {
    std::string address = foundDevice.getAddress().toString();
    std::string name = foundDevice.getName();
    std::string mData = foundDevice.getManufacturerData();
    int rssi = foundDevice.getRSSI();
    Serial.print("BLE Device found -> Address: ");
    Serial.print(address.c_str());
    Serial.print(", Name: ");
    Serial.print(name.c_str());
    Serial.print(", RSSI: ");
    Serial.print(rssi);
    Serial.print(", Manufacturer Data: ");
    dump(mData);
    // check the device is a light, and using the default key
    // check response
    if (mData.size() == 18) {
      std::string type = mData.substr(12,2);
      std::string key = mData.substr(14,4);
      bool knownType = false;
      for (int i = 0; i < lightTypes.size(); i++) {
        if (doesStringMatchBytes(type, lightTypes[i].code)) {
          Serial.printf(", It's a %s light!", lightTypes[i].name.c_str());
          knownType = true;
          break;
        }
      }
      if (knownType && doesStringMatchBytes(key, default_key)) {
        Serial.print(", Using the default key!");
        bool alreadyKnown = false;
        for (int i = 0; i < myLights.size(); i++) {
          if (myLights[i].device.getAddress().toString() == foundDevice.getAddress().toString()) {
            // we already know about this device, so ignore it
            alreadyKnown = true;
            break;
          }
        }
        if (alreadyKnown == false) {
          Serial.print(", Stored it!");
          LightDevice light;
          light.device = foundDevice;
          light.type[0] = type[0];
          light.type[1] = type[1];
          myLights.push_back(light);
        }
      }
    }
    Serial.println("");
  }
};

class AddLightCallback: public BLEAdvertisedDeviceCallbacks
{
  void onResult(BLEAdvertisedDevice foundDevice)
  {
    std::string address = foundDevice.getAddress().toString();
    std::string name = foundDevice.getName();
    std::string mData = foundDevice.getManufacturerData();
    int rssi = foundDevice.getRSSI();
    Serial.print("BLE Device found -> Address: ");
    Serial.print(address.c_str());
    Serial.print(", Name: ");
    Serial.print(name.c_str());
    Serial.print(", RSSI: ");
    Serial.print(rssi);
    Serial.print(", Manufacturer Data: ");
    dump(mData);
    // check response
    if (mData.size() == 18) {
      for (int i = 0; i < myLights.size(); i++) {
        if (myLights[i].device.getAddress().toString() == foundDevice.getAddress().toString()) {
          Serial.print(", It's one of our lights!");
          // check it's not using the default key
          std::string key = mData.substr(14,4);
          if (doesStringMatchBytes(key, default_key)) {
            Serial.print(", still using the default key (ignore)");
          } else {
            Serial.print(", with the new key");
            if (myLights[i].isRegistered) {
              Serial.print(", but it's already registered!");
            } else {
              myLights[i].isRegistered = true;
              // get the light number from the light itself
              uint8_t cleanManufacturerData[12];
              uint8_t* manufacturerData = (uint8_t*)foundDevice.getManufacturerData().substr(2).c_str();
              // use the key to clean it
              for (int j = 0; j < 12; j++) {
                cleanManufacturerData[j] = my_key[j & 3] ^ manufacturerData[4 + j];
              }
              Serial.print(", clean manufacturer data: "); dump(cleanManufacturerData, 12);
              myLights[i].number = cleanManufacturerData[1];
              myLights[i].id = myLights[i].device.getAddress().toString().substr(3,2) + myLights[i].device.getAddress().toString().substr(0,2);
              myLights[i].name = "Light_" + myLights[i].id;
              // enable features based on type
              std::string typeName = "";
              for (int j = 0; j < lightTypes.size(); j++) {
                if (myLights[i].type[0] == lightTypes[j].code[0] && myLights[i].type[1] == lightTypes[j].code[1]) {
                  typeName = lightTypes[j].name;
                  break;
                }
              }
              // create the HA object
              if (typeName == "RGBW") {
                HALight* light = new HALight(myLights[i].id.c_str(), HALight::BrightnessFeature | HALight::ColorTemperatureFeature | HALight::RGBFeature);
                light->setName(myLights[i].name.c_str());
                light->onStateCommand(onStateCommand);
                light->onBrightnessCommand(onBrightnessCommand);
                light->onColorTemperatureCommand(onColorTemperatureCommand);
                light->onRGBColorCommand(onRGBColorCommand);
                light->setBrightnessScale(127);
                light->setBrightness(127);
                myLights[i].light = light;
              } else if (typeName == "RGB") {
                HALight* light = new HALight(myLights[i].id.c_str(), HALight::BrightnessFeature | HALight::RGBFeature);
                light->setName(myLights[i].name.c_str());
                light->onStateCommand(onStateCommand);
                light->onBrightnessCommand(onBrightnessCommand);
                light->onRGBColorCommand(onRGBColorCommand);
                light->setBrightnessScale(127);
                light->setBrightness(127);
                myLights[i].light = light;
              } else {
                // "Smart" - no additional features
                HALight* light = new HALight(myLights[i].id.c_str());
                light->setName(myLights[i].name.c_str());
                light->onStateCommand(onStateCommand);
                myLights[i].light = light;
              }
              Serial.printf(", created light ");
              Serial.printf(myLights[i].light->uniqueId());
            }
            Serial.println("");
          }
          break;
        }
      }
    }
    Serial.println("");
  }
};

void scan()
{
  Serial.println("Send wake command");
  uint8_t data[] = { 0x00, 0x00, 0x00, 0x00, 0x00, 0x00 };
  const uint8_t key[] = { 0x00, 0x00, 0x00, 0x00 };
  uint8_t* rfPayload = 0;
  uint8_t rfPayloadLength = do_generate_command(0, data, 6, key, false, true, 0, rfPayload);
  std::string serviceData = getServiceData(rfPayloadLength, rfPayload);
  BLEAdvertisementData oAdvertisementData = BLEAdvertisementData();
  oAdvertisementData.setFlags(0x04); // BR_EDR_NOT_SUPPORTED 0x04
  oAdvertisementData.addData(serviceData);
  pAdvertising->setAdvertisementData(oAdvertisementData);
  pAdvertising->start();
  Serial.println("Scan for lights");
  pBLEScan = BLEDevice::getScan();
  pBLEScan->setAdvertisedDeviceCallbacks(new AddDeviceCallback());
  pBLEScan->setInterval(500);
  pBLEScan->setWindow(500);
  pBLEScan->setActiveScan(true);
  pBLEScan->start(BLESCAN_DURATION, false);
  pAdvertising->stop();
}

void addLight(uint8_t lightNumber, LightDevice light)
{
  uint8_t data[12];
  std::string lightMac = light.device.getManufacturerData().substr(6, 6);
  Serial.printf("Setting key on light %d, MAC: ", lightNumber); dump(lightMac); Serial.print("\n");
  Serial.print("new key: "); dump(my_key, 4); Serial.print("\n");
  for (int i = 0; i < 6; i++) data[i] = (uint8_t)lightMac[i]; // mac address
  data[6] = lightNumber; // light id - we're requesting that it's set to this
  data[7] = 0x01; // group id
  data[8] = my_key[0];
  data[9] = my_key[1];
  data[10] = my_key[2];
  data[11] = my_key[3];
  uint8_t* rfPayload = 0;
  uint8_t rfPayloadLength = do_generate_command(2, data, 12, default_key, false, true, 0, rfPayload);
  std::string serviceData = getServiceData(rfPayloadLength, rfPayload);
  BLEAdvertisementData oAdvertisementData = BLEAdvertisementData();
  oAdvertisementData.setFlags(0x04);
  oAdvertisementData.addData(serviceData);
  pAdvertising->setAdvertisementData(oAdvertisementData);
  pAdvertising->setMinInterval(50);
  pAdvertising->setMaxInterval(50);
  pAdvertising->start();
  // get light response
  pBLEScan = BLEDevice::getScan();
  pBLEScan->setAdvertisedDeviceCallbacks(new AddLightCallback());
  pBLEScan->setInterval(50);
  pBLEScan->setWindow(50);
  pBLEScan->setActiveScan(true);
  pBLEScan->start(1);
  pAdvertising->stop();
}

void addLights()
{
  scan();
  // wait before adding lights, as they seem to need a brief pause after the scan
  delay(1000);
  for (int i = 0; i < myLights.size(); i++) {
    addLight(i + 1, myLights[i]);
  }
}

// Register existing lights from the CONFIGURATION table so the bridge can
// control them directly with the configured key (no discovery / re-keying).
void setupPreconfiguredLights()
{
  for (int i = 0; i < PRECONFIGURED_LIGHT_COUNT; i++) {
    const PreconfiguredLight& cfg = preconfiguredLights[i];
    LightDevice light;
    light.type[0] = cfg.type[0];
    light.type[1] = cfg.type[1];
    light.isRegistered = true;
    light.number = cfg.number;
    light.id = cfg.id;
    light.name = cfg.name;

    // determine the light type from the code
    std::string typeName = "";
    for (int j = 0; j < lightTypes.size(); j++) {
      if (light.type[0] == lightTypes[j].code[0] && light.type[1] == lightTypes[j].code[1]) {
        typeName = lightTypes[j].name;
        break;
      }
    }

    // create the HA object based on type
    if (typeName == "RGBW") {
      HALight* l = new HALight(cfg.id, HALight::BrightnessFeature | HALight::ColorTemperatureFeature | HALight::RGBFeature);
      l->setName(cfg.name);
      l->onStateCommand(onStateCommand);
      l->onBrightnessCommand(onBrightnessCommand);
      l->onColorTemperatureCommand(onColorTemperatureCommand);
      l->onRGBColorCommand(onRGBColorCommand);
      l->setBrightnessScale(127);
      l->setBrightness(127);
      light.light = l;
    } else if (typeName == "RGB") {
      HALight* l = new HALight(cfg.id, HALight::BrightnessFeature | HALight::RGBFeature);
      l->setName(cfg.name);
      l->onStateCommand(onStateCommand);
      l->onBrightnessCommand(onBrightnessCommand);
      l->onRGBColorCommand(onRGBColorCommand);
      l->setBrightnessScale(127);
      l->setBrightness(127);
      light.light = l;
    } else {
      // "Smart" - no additional features
      HALight* l = new HALight(cfg.id);
      l->setName(cfg.name);
      l->onStateCommand(onStateCommand);
      light.light = l;
    }

    myLights.push_back(light);
    Serial.printf("Preconfigured Light %d - ", light.number);
    Serial.println(light.light->uniqueId());
  }
}

void setup() {
  pinMode (ledPin, OUTPUT);
  // turn on to show we're still in setup (and are adding for lights)
  digitalWrite (ledPin, HIGH);
  Serial.begin(115200);
  // set the key: fixed (configured) for joining an existing mesh, or random
  if (USE_RANDOM_KEY) {
    uint32_t new_key = esp_random();
    my_key[0] = new_key & 0xFF;
    my_key[1] = (new_key >> 8) & 0xFF;
    my_key[2] = (new_key >> 16) & 0xFF;
    my_key[3] = (new_key >> 24) & 0xFF;
  } else {
    my_key[0] = MY_KEY_0;
    my_key[1] = MY_KEY_1;
    my_key[2] = MY_KEY_2;
    my_key[3] = MY_KEY_3;
  }
  Serial.print("Key: "); dump(my_key, 4); Serial.print("\n");
  WiFi.macAddress(mac);
  device.setUniqueId(mac, sizeof(mac));
  device.setName("BRMesh");
  device.setManufacturer("BRMesh");
  device.setModel("BRMesh");
  mqtt = new HAMqtt(client, device, 64); // 64 device types: 41 lights + group entities (default limit is 24)
  Serial.printf("ESP32 MAC: %02X:%02X:%02X:%02X:%02X:%02X\n", mac[0],mac[1],mac[2],mac[3],mac[4],mac[5]);
  // Create the BLE Device (empty name so no name AD is auto-added)
  BLEDevice::init("");
  // Boost BLE TX power to maximum so the lights/sniffer can receive it.
  esp_ble_tx_power_set(ESP_BLE_PWR_TYPE_ADV, ESP_PWR_LVL_P9);
  esp_ble_tx_power_set(ESP_BLE_PWR_TYPE_DEFAULT, ESP_PWR_LVL_P9);
  pAdvertising = BLEDevice::getAdvertising();
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  while (WiFi.status() != WL_CONNECTED) {
    delay(500); // waiting for the connection
    Serial.println("Connecting to WiFi...");
  }
  // add the lights: use preconfigured existing lights, or auto-discover/re-key
  if (PRECONFIGURED_LIGHT_COUNT > 0) {
    setupPreconfiguredLights();
  } else {
    addLights();
  }
  // print the added lights with their IDs
  for (int i = 0; i < myLights.size(); i++) {
    if (myLights[i].isRegistered) {
      Serial.printf("Light %d - ", myLights[i].number);
      Serial.println(myLights[i].light->uniqueId());
    }
  }
  // finished adding lights
  digitalWrite (ledPin, LOW);

  // "All lights" group on/off switch (sends a single all-on/all-off command)
  allSwitch = new HASwitch("all_lights");
  allSwitch->setName("All Lights");
  allSwitch->onCommand(onAllSwitchCommand);

  // "All lights" group brightness slider (sends a D5 group-brightness command)
  groupBrightness = new HANumber("all_lights_brightness");
  groupBrightness->setName("All Lights Brightness");
  groupBrightness->setIcon("mdi:brightness-percent");
  groupBrightness->setUnitOfMeasurement("%");
  groupBrightness->setMin(0);
  groupBrightness->setMax(100);
  groupBrightness->setStep(1);
  groupBrightness->setMode(HANumber::ModeSlider);
  groupBrightness->onCommand(onGroupBrightnessCommand);

  // "All lights" group color entity (sends a family 0x05 group-color command)
  groupColor = new HALight("all_lights_color", HALight::RGBFeature);
  groupColor->setName("All Lights Color");
  groupColor->onRGBColorCommand(onGroupColorCommand);
  groupColor->setBrightnessScale(127);
  groupColor->setBrightness(127);

  Serial.println("Starting MQTT");
  mqtt->setBufferSize(2048); // discovery messages can exceed the 256-byte default
  mqtt->begin(MQTT_BROKER_ADDR, 1883);
  Serial.println("Ready");
}

void loop()
{
  mqtt->loop();

  // Serial test commands: send the EXACT captured app payloads (byte-for-byte)
  // so we can compare against the sniffer and see if the lights respond.
  if (Serial.available()) {
    String cmd = Serial.readStringUntil('\n');
    cmd.trim();
    if (cmd == "allon") {
      uint8_t d[12] = {0x43,0x2A,0xA8,0x00,0x80,0x00,0x00,0x00,0x00,0x00,0x00,0x00};
      sendAppCommand(d, 12);
      Serial.println(">>> sent plaintext ALL ON (family D5)");
    } else if (cmd == "alloff") {
      uint8_t d[12] = {0x43,0x2A,0xA8,0x00,0x00,0x00,0x00,0x00,0x00,0x00,0x00,0x00};
      sendAppCommand(d, 12);
      Serial.println(">>> sent plaintext ALL OFF (family D5)");
    } else if (cmd.startsWith("gb ")) {
      // Group brightness test: gb <0-100>  -> sends a D5 group-brightness command
      uint8_t pct = (uint8_t)cmd.substring(3).toInt();
      sendGroupBrightness(pct);
      Serial.printf(">>> sent group brightness %d%%\n", pct);
    } else if (cmd == "red") {
      uint8_t d[12] = {0x72,0x02,0x81,0x00,0xFF,0x00,0x00,0x00,0x00,0x00,0x00,0x00};
      sendAppCommand(d, 12);
      Serial.println(">>> sent plaintext RED (dev5, family E4)");
    } else if (cmd == "green") {
      uint8_t d[12] = {0x72,0x02,0x81,0x00,0x00,0xFF,0x00,0x00,0x00,0x00,0x00,0x00};
      sendAppCommand(d, 12);
      Serial.println(">>> sent plaintext GREEN (dev5, family E4)");
    } else if (cmd == "blue") {
      uint8_t d[12] = {0x72,0x02,0x81,0xFF,0x00,0x00,0x00,0x00,0x00,0x00,0x00,0x00};
      sendAppCommand(d, 12);
      Serial.println(">>> sent plaintext BLUE (dev5, family E4)");
    } else if (cmd == "wifioff") {
      // Turn off the WiFi radio so BLE has the radio to itself (coexistence test).
      WiFi.mode(WIFI_OFF);
      Serial.println(">>> WiFi OFF - BLE now has the radio");
    } else if (cmd == "cont") {
      // Continuous-advertise test: keep transmitting the plaintext ALL-ON payload
      // so nRF Connect / a scanner can see whether the bridge transmits correctly.
      uint8_t d[12] = {0x43,0x2A,0xA8,0x00,0x80,0x00,0x00,0x00,0x00,0x00,0x00,0x00};
      advertiseAppPayload(d, 12, true); // continuous - do NOT stop
      Serial.println(">>> continuous advertising test payload (FF F0 FF)");
    } else if (cmd.length() > 0) {
      Serial.printf("Unknown serial cmd: %s\n", cmd.c_str());
    }
  }
}
