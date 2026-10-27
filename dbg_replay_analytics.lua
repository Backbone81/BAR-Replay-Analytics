local widget = widget ---@type Widget

function widget:GetInfo()
	return {
		name = "Replay Analytics",
		desc = "Logs unit, economy and builder events of a replay to <replay name>_events.jsonl",
		author = "Backbone",
		date = "October 2026",
		license = "GNU GPL, v2 or later",
		layer = -100000,
		enabled = true,
	}
end

-- Logs the events of a replay played back in spring-headless. Unsynced, so it cannot desync.
-- Active only in a headless replay; in a normal client, also when watching a replay, it removes itself.
-- Writes one JSON object per line to events/<replay name>_events.jsonl in the widget's own folder (BAR lets user widgets
-- write only under LuaUI/Config/ and LuaUI/Widgets/). Switches to full view, raises the speed
-- limit (the pre-game start position phase of a replay otherwise plays in real time), skips to the end of the replay
-- once the game runs, and quits the engine when the game is over or the replay stops advancing.

--------------------------------------------------------------------------------
-- Configuration
--------------------------------------------------------------------------------

local RESOURCES_SECONDS = 5 -- "resources" events
local BUILDER_SAMPLE_SECONDS = 0.5 -- "builderState" events are written on change only
local ARMY_POSITIONS_SECONDS = 30 -- "armyPositions" events
local STALL_QUIT_SECONDS = 20 -- the replay has ended when frames stop advancing
local PREGAME_QUIT_SECONDS = 300

--------------------------------------------------------------------------------
-- Engine API and state
--------------------------------------------------------------------------------

local spDiffTimers = Spring.DiffTimers
local spEcho = Spring.Echo
local spGetAIInfo = Spring.GetAIInfo
local spGetGaiaTeamID = Spring.GetGaiaTeamID
local spGetGameFrame = Spring.GetGameFrame
local spGetPlayerInfo = Spring.GetPlayerInfo
local spGetTeamInfo = Spring.GetTeamInfo
local spGetTeamList = Spring.GetTeamList
local spGetTeamResources = Spring.GetTeamResources
local spGetTeamUnitCount = Spring.GetTeamUnitCount
local spGetTeamUnits = Spring.GetTeamUnits
local spGetTimer = Spring.GetTimer
local spGetUnitCommandCount = Spring.GetUnitCommandCount
local spGetUnitCurrentCommand = Spring.GetUnitCurrentCommand
local spGetUnitDefID = Spring.GetUnitDefID
local spGetUnitHealth = Spring.GetUnitHealth
local spGetUnitIsBeingBuilt = Spring.GetUnitIsBeingBuilt
local spGetUnitIsBuilding = Spring.GetUnitIsBuilding
local spGetUnitPosition = Spring.GetUnitPosition
local spGetUnitWorkerTask = Spring.GetUnitWorkerTask
local spIsReplay = Spring.IsReplay
local spSendCommands = Spring.SendCommands

local mathAbs = math.abs
local mathFloor = math.floor
local mathHuge = math.huge
local stringFormat = string.format

local GAME_SPEED = Game.gameSpeed
local RESOURCES_FRAMES = RESOURCES_SECONDS * GAME_SPEED
local BUILDER_SAMPLE_FRAMES = mathFloor(BUILDER_SAMPLE_SECONDS * GAME_SPEED)
local ARMY_POSITIONS_FRAMES = ARMY_POSITIONS_SECONDS * GAME_SPEED

-- The replay's file name without .sdfz (Spring.GetReplayFilePath() returns nil once the replay has ended).
local EVENTS_FILE_NAME = (Game.demoPlayName or "replay") .. "_events.jsonl"

-- Unit def lookups, filled in Initialize.
local isFactoryDef = {}
local isBuilderDef = {} -- mobile builders: commanders and constructors
local isNanoDef = {} -- builders that can't move
local isArmyDef = {} -- mobile units that are not builders
local isUnitDefLogged = {}

local eventsFile -- nil while not logging
local teamIDs = {} -- set of every team but Gaia

-- unitID -> teamID
local factories = {}
local builders = {}
local nanos = {}

-- unitID -> last logged "builderState" fields
local builderStates = {}
local builderCommands = {}
local builderTargets = {}

local lastFrame = -1
local lastFrameTimer -- when the frame last advanced; nil until Initialize
local isSpeedRaised = false
local isSkipSent = false
local isQuitting = false

--------------------------------------------------------------------------------
-- Event output
--------------------------------------------------------------------------------

-- Own encoder: BAR's Json.encode writes NaN and infinity as invalid JSON and floats with 14 digits.
local function encodeJson(value)
	local valueType = type(value)
	if valueType == "number" then
		if value ~= value or value == mathHuge or value == -mathHuge then
			return "null"
		end
		if value == mathFloor(value) and mathAbs(value) < 1e15 then
			return stringFormat("%d", value)
		end
		return stringFormat("%.3f", value)
	elseif valueType == "string" then
		local escaped = value:gsub('[%c"\\]', function(char)
			return stringFormat("\\u%04x", char:byte())
		end)
		return '"' .. escaped .. '"'
	elseif valueType == "boolean" then
		return tostring(value)
	elseif valueType == "table" then
		local parts = {}
		if #value > 0 or next(value) == nil then
			for i = 1, #value do
				parts[i] = encodeJson(value[i])
			end
			return "[" .. table.concat(parts, ",") .. "]"
		end
		for key, element in pairs(value) do
			parts[#parts + 1] = encodeJson(tostring(key)) .. ":" .. encodeJson(element)
		end
		return "{" .. table.concat(parts, ",") .. "}"
	end
	return "null"
end

local function writeEvent(event)
	if not eventsFile then
		return
	end
	event.frame = spGetGameFrame()
	eventsFile:write(encodeJson(event), "\n")
end

local function closeEventsFile(reason)
	if not eventsFile then
		return
	end
	writeEvent({ event = "end", reason = reason })
	eventsFile:close()
	eventsFile = nil
end

--------------------------------------------------------------------------------
-- Units and teams
--------------------------------------------------------------------------------

-- Map position in elmos, rounded down, as { x, z }.
local function getUnitPosition(unitID)
	local x, _, z = spGetUnitPosition(unitID)
	if x then
		return { mathFloor(x), mathFloor(z) }
	end
end

-- Logs a "unitDef" event the first time a unit type shows up, so the event file needs no unit def files to read.
local function getUnitDefName(unitDefID)
	local unitDef = unitDefID and UnitDefs[unitDefID]
	if not unitDef then
		return nil
	end
	if not isUnitDefLogged[unitDefID] then
		isUnitDefLogged[unitDefID] = true
		local customParams = unitDef.customParams
		writeEvent({
			event = "unitDef",
			name = unitDef.name,
			humanName = unitDef.translatedHumanName or unitDef.humanName,
			metalCost = unitDef.metalCost,
			energyCost = unitDef.energyCost,
			buildTime = unitDef.buildTime,
			techLevel = tonumber(customParams.techlevel) or 1,
			unitGroup = customParams.unitgroup,
			isFactory = unitDef.isFactory or nil,
			isBuilder = (unitDef.isBuilder and not unitDef.isFactory) or nil,
			isCommander = customParams.iscommander and true or nil,
			isMobile = (unitDef.canMove and not unitDef.isFactory) or nil,
			buildDistance = (unitDef.isBuilder and not unitDef.isFactory) and unitDef.buildDistance or nil,
			canResurrect = unitDef.canResurrect or nil,
			canFly = unitDef.canFly or nil,
			extractsMetal = unitDef.extractsMetal > 0 and unitDef.extractsMetal or nil,
			energyMake = unitDef.energyMake > 0 and unitDef.energyMake or nil,
			windGenerator = unitDef.windGenerator > 0 and unitDef.windGenerator or nil,
			maxWeaponRange = unitDef.maxWeaponRange > 0 and unitDef.maxWeaponRange or nil,
		})
	end
	return unitDef.name
end

local function classifyUnitDefs()
	for unitDefID, unitDef in pairs(UnitDefs) do
		if unitDef.isFactory then
			isFactoryDef[unitDefID] = true
		elseif unitDef.isBuilder and unitDef.canMove then
			isBuilderDef[unitDefID] = true
		elseif unitDef.isBuilder then
			isNanoDef[unitDefID] = true
		elseif unitDef.canMove then
			isArmyDef[unitDefID] = true
		end
	end
end

local function getTeamName(teamID, leaderID, isAI)
	if isAI then
		local _, aiName, _, shortName = spGetAIInfo(teamID)
		return (aiName or "AI") .. " (" .. tostring(shortName) .. ")"
	end
	return spGetPlayerInfo(leaderID, false)
end

-- Fills teamIDs and returns the teams for the "init" event.
local function collectTeams()
	local teams = {}
	local gaiaTeamID = spGetGaiaTeamID()
	for _, teamID in ipairs(spGetTeamList()) do
		if teamID ~= gaiaTeamID then
			local _, leaderID, _, isAI, side, allyTeamID = spGetTeamInfo(teamID, false)
			teamIDs[teamID] = true
			teams[#teams + 1] = {
				teamID = teamID,
				allyTeamID = allyTeamID,
				isAI = isAI,
				side = side,
				name = getTeamName(teamID, leaderID, isAI),
			}
		end
	end
	return teams
end

local function addUnit(unitID, unitDefID, teamID)
	if isFactoryDef[unitDefID] then
		factories[unitID] = teamID
	elseif isBuilderDef[unitDefID] then
		builders[unitID] = teamID
	elseif isNanoDef[unitDefID] then
		nanos[unitID] = teamID
	end
end

local function removeUnit(unitID)
	factories[unitID] = nil
	builders[unitID] = nil
	nanos[unitID] = nil
	builderStates[unitID] = nil
	builderCommands[unitID] = nil
	builderTargets[unitID] = nil
end

--------------------------------------------------------------------------------
-- Sampling
--------------------------------------------------------------------------------

-- Logs a "builderState" event when a finished builder's state changes: "working" (command: build = -unitDefID,
-- 40 repair or assist, 90 reclaim, 125 resurrect; targetID: unit or feature), "busy" (orders but no worker task, e.g.
-- walking; command: the current one) or "idle" (no orders).
local function logBuilderState(unitID)
	if spGetUnitIsBeingBuilt(unitID) then
		return
	end
	local state, command, target
	local taskCommand, taskTarget = spGetUnitWorkerTask(unitID)
	if taskCommand then
		state, command, target = "working", taskCommand, taskTarget
	elseif (spGetUnitCommandCount(unitID) or 0) > 0 then
		state, command = "busy", spGetUnitCurrentCommand(unitID)
	else
		state = "idle"
	end
	if builderStates[unitID] == state and builderCommands[unitID] == command and builderTargets[unitID] == target then
		return
	end
	builderStates[unitID] = state
	builderCommands[unitID] = command
	builderTargets[unitID] = target
	writeEvent({ event = "builderState", unitID = unitID, state = state, command = command, targetID = target })
end

local function logResources()
	local builderCounts, idleBuilderCounts, factoryCounts, idleFactoryCounts = {}, {}, {}, {}
	for teamID in pairs(teamIDs) do
		builderCounts[teamID], idleBuilderCounts[teamID] = 0, 0
		factoryCounts[teamID], idleFactoryCounts[teamID] = 0, 0
	end
	for unitID, teamID in pairs(builders) do
		if teamIDs[teamID] and not spGetUnitIsBeingBuilt(unitID) then
			builderCounts[teamID] = builderCounts[teamID] + 1
			if (spGetUnitCommandCount(unitID) or 0) == 0 and not spGetUnitIsBuilding(unitID) then
				idleBuilderCounts[teamID] = idleBuilderCounts[teamID] + 1
			end
		end
	end
	for unitID, teamID in pairs(factories) do
		if teamIDs[teamID] and not spGetUnitIsBeingBuilt(unitID) then
			factoryCounts[teamID] = factoryCounts[teamID] + 1
			if not spGetUnitIsBuilding(unitID) then
				idleFactoryCounts[teamID] = idleFactoryCounts[teamID] + 1
			end
		end
	end
	for teamID in pairs(teamIDs) do
		local metalCurrent, metalStorage, metalPull, metalIncome, metalExpense = spGetTeamResources(teamID, "metal")
		local energyCurrent, energyStorage, energyPull, energyIncome, energyExpense =
			spGetTeamResources(teamID, "energy")
		writeEvent({
			event = "resources",
			teamID = teamID,
			metal = {
				current = metalCurrent,
				storage = metalStorage,
				pull = metalPull,
				income = metalIncome,
				expense = metalExpense,
			},
			energy = {
				current = energyCurrent,
				storage = energyStorage,
				pull = energyPull,
				income = energyIncome,
				expense = energyExpense,
			},
			unitCount = spGetTeamUnitCount(teamID),
			builderCount = builderCounts[teamID],
			idleBuilderCount = idleBuilderCounts[teamID],
			factoryCount = factoryCounts[teamID],
			idleFactoryCount = idleFactoryCounts[teamID],
		})
	end
end

local function logArmyPositions()
	for teamID in pairs(teamIDs) do
		local units = {}
		for _, unitID in ipairs(spGetTeamUnits(teamID) or {}) do
			local unitDefID = spGetUnitDefID(unitID)
			if isArmyDef[unitDefID] then
				local position = getUnitPosition(unitID)
				if position then
					units[#units + 1] = { unitDef = UnitDefs[unitDefID].name, position = position }
				end
			end
		end
		writeEvent({ event = "armyPositions", teamID = teamID, units = units })
	end
end

local function quit(reason)
	if isQuitting then
		return
	end
	isQuitting = true
	closeEventsFile(reason)
	spEcho("Replay Analytics: done (" .. reason .. ")")
	spSendCommands("quitforce")
end

--------------------------------------------------------------------------------
-- Call-ins
--------------------------------------------------------------------------------

function widget:Initialize()
	if not (spIsReplay() and Platform.isHeadless) then
		widgetHandler:RemoveWidget()
		return
	end
	local eventsFolder = (widget.whInfo.filename:match("^(.*[/\\])") or "") .. "events/"
	Spring.CreateDir(eventsFolder)
	local eventsPath = eventsFolder .. EVENTS_FILE_NAME
	eventsFile = io.open(eventsPath, "w")
	if not eventsFile then
		spEcho("Replay Analytics: cannot write " .. eventsPath)
		widgetHandler:RemoveWidget()
		return
	end
	classifyUnitDefs()
	spSendCommands("specfullview 1")
	writeEvent({
		event = "init",
		map = Game.mapName,
		mapSizeX = Game.mapSizeX,
		mapSizeZ = Game.mapSizeZ,
		game = Game.gameName .. " " .. Game.gameVersion,
		teams = collectTeams(),
	})
	lastFrameTimer = spGetTimer()
end

function widget:Shutdown()
	closeEventsFile("shutdown")
end

function widget:Update()
	if isQuitting or not lastFrameTimer then
		return
	end
	-- A server command the local player may send; the speed limit also applies before the game starts.
	if not isSpeedRaised then
		isSpeedRaised = true
		spSendCommands("setmaxspeed 1000", "setspeed 1000")
	end
	local frame = spGetGameFrame()
	if frame ~= lastFrame then
		lastFrame = frame
		lastFrameTimer = spGetTimer()
		return
	end
	local quitSeconds = frame > 0 and STALL_QUIT_SECONDS or PREGAME_QUIT_SECONDS
	if spDiffTimers(spGetTimer(), lastFrameTimer) > quitSeconds then
		quit(frame > 0 and "stalled" or "never started")
	end
end

function widget:GameFrame(frame)
	-- The skip only works once the game runs; the server then sends the rest of the replay as fast as it can.
	if not isSkipSent and frame >= 1 then
		isSkipSent = true
		spSendCommands("skip f99999999")
	end
	if frame % BUILDER_SAMPLE_FRAMES == 0 then
		for unitID in pairs(builders) do
			logBuilderState(unitID)
		end
		for unitID in pairs(nanos) do
			logBuilderState(unitID)
		end
	end
	if frame % RESOURCES_FRAMES == 0 then
		logResources()
	end
	if frame % ARMY_POSITIONS_FRAMES == 0 then
		logArmyPositions()
	end
end

function widget:UnitCreated(unitID, unitDefID, teamID, builderID)
	addUnit(unitID, unitDefID, teamID)
	writeEvent({
		event = "unitCreated",
		unitID = unitID,
		unitDef = getUnitDefName(unitDefID),
		teamID = teamID,
		position = getUnitPosition(unitID),
		builderID = builderID,
		builderDef = builderID and getUnitDefName(spGetUnitDefID(builderID)),
	})
end

function widget:UnitFinished(unitID, unitDefID, teamID)
	writeEvent({ event = "unitFinished", unitID = unitID, unitDef = getUnitDefName(unitDefID), teamID = teamID })
end

function widget:UnitDestroyed(unitID, unitDefID, teamID, _, attackerDefID, attackerTeamID, weaponDefID)
	local _, _, _, _, buildProgress = spGetUnitHealth(unitID)
	local weaponDef = weaponDefID and WeaponDefs[weaponDefID]
	writeEvent({
		event = "unitDestroyed",
		unitID = unitID,
		unitDef = getUnitDefName(unitDefID),
		teamID = teamID,
		position = getUnitPosition(unitID),
		buildProgress = buildProgress,
		attackerDef = getUnitDefName(attackerDefID),
		attackerTeamID = attackerTeamID,
		weapon = weaponDef and weaponDef.name,
	})
	removeUnit(unitID)
end

function widget:UnitGiven(unitID, unitDefID, newTeamID, oldTeamID)
	removeUnit(unitID)
	addUnit(unitID, unitDefID, newTeamID)
	writeEvent({
		event = "unitGiven",
		unitID = unitID,
		unitDef = getUnitDefName(unitDefID),
		teamID = newTeamID,
		oldTeamID = oldTeamID,
	})
end

function widget:GameOver(winningAllyTeams)
	writeEvent({ event = "gameOver", winningAllyTeamIDs = winningAllyTeams })
	quit("gameover")
end
